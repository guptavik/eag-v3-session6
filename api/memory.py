"""Memory layer — durable store, keyword recall, classifier-based
remember, plus the content-addressable artifact store.

Three responsibilities, two file-system trees:

1. **AgentMemory** — list of `MemoryItem` in `state/memory.json`.
   Durable across runs. The Query C contract lives here: run 1
   writes a `fact`, run 2 reads it.

2. **ArtifactStore** — content-addressable blob store at
   `state/artifacts/`. Action offloads large tool payloads here;
   Perception can later attach them to goals.

3. **Memory.remember(user_query)** — LLM classifier that pre-extracts
   facts/preferences from the user's message at the top of every
   `agent6.run()`. Runs *before* the iteration loop so Query C run 1
   captures the birthday before Decision is even consulted.

All file writes are atomic (tmp + os.replace) so a kill -9 mid-write
cannot corrupt `state/memory.json`. State is fully cleanable with
`rm -rf state/` — this is the assignment-mandated cleanup story.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import _gateway_path  # noqa: F401  — side-effect: adds mcp-server/ to sys.path
from llm_gatewayV3.client import LLM

from schemas import (
    ARTIFACT_ID_PREFIX,
    Artifact,
    Goal,
    MemoryItem,
    new_memory_id,
)

log = logging.getLogger(__name__)

# How many characters of sha256 to include in the artifact id. 16 hex
# chars = 64 bits of randomness, collision-safe at any single-user
# session scale.
ARTIFACT_ID_HEX_LEN = 16
MEMORY_FILE_VERSION = 1


# ---------------------------------------------------------------------------
# ArtifactStore — content-addressable blob storage on disk.
# ---------------------------------------------------------------------------


class ArtifactStore:
    """Bytes by content-hash. Metadata in `<prefix>.json`, payload in
    `<prefix>.bin` (or `.md` / `.txt` / `.html` when content_type maps
    cleanly to a human-readable extension)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    # -- write -------------------------------------------------------

    def store(
        self,
        content: bytes,
        *,
        content_type: str,
        source: str,
        descriptor: str,
    ) -> Artifact:
        digest = hashlib.sha256(content).hexdigest()[:ARTIFACT_ID_HEX_LEN]
        artifact_id = f"{ARTIFACT_ID_PREFIX}{digest}"
        ext = _content_extension(content_type)

        bin_path = self.root / f"{digest}{ext}"
        meta_path = self.root / f"{digest}.json"

        # If the same content was already stored, refresh the metadata
        # (source/descriptor may differ between fetches) but keep the
        # bytes — content-addressable invariant.
        if not bin_path.exists():
            _atomic_write_bytes(bin_path, content)

        artifact = Artifact(
            id=artifact_id,
            content_type=content_type,
            size_bytes=len(content),
            source=source,
            descriptor=descriptor,
        )
        _atomic_write_text(meta_path, artifact.model_dump_json(indent=2))
        return artifact

    # -- read --------------------------------------------------------

    def read_bytes(self, artifact_id: str) -> bytes:
        digest = self._digest_from_id(artifact_id)
        candidates = sorted(self.root.glob(f"{digest}.*"))
        for path in candidates:
            if path.suffix != ".json":
                return path.read_bytes()
        raise FileNotFoundError(f"artifact {artifact_id} (no bin file)")

    def read_text(self, artifact_id: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(artifact_id).decode(encoding, errors="replace")

    def read_meta(self, artifact_id: str) -> Artifact:
        digest = self._digest_from_id(artifact_id)
        meta_path = self.root / f"{digest}.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"artifact {artifact_id} (no meta)")
        return Artifact.model_validate_json(meta_path.read_text("utf-8"))

    def _digest_from_id(self, artifact_id: str) -> str:
        if not artifact_id.startswith(ARTIFACT_ID_PREFIX):
            raise ValueError(f"not an artifact id: {artifact_id!r}")
        return artifact_id[len(ARTIFACT_ID_PREFIX) :]


# ---------------------------------------------------------------------------
# AgentMemory — durable MemoryItem store + recall + remember.
# ---------------------------------------------------------------------------


class AgentMemory:
    """List of MemoryItem, persisted at state/memory.json.

    Read is in-memory after the initial load. Write goes through
    `_persist()` which does atomic file-replace. The store is small
    enough (single-user, < 10K items realistic) that we don't bother
    with indexes — keyword recall is a linear scan."""

    def __init__(self, path: Path, *, llm: LLM | None = None) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._items: list[MemoryItem] = self._load()
        # Lazy LLM — only constructed when remember() is called, so unit
        # tests of recall() can run without a gateway.
        self._llm = llm

    # -- properties / iteration -------------------------------------

    @property
    def items(self) -> list[MemoryItem]:
        return list(self._items)

    # -- recall ------------------------------------------------------

    def recall(self, keywords: list[str], *, limit: int = 20) -> list[MemoryItem]:
        """Substring-match the input keywords (case-insensitive)
        against each MemoryItem's keywords + descriptor. Items
        matching more keywords rank higher; ties broken by recency.
        Items with `kind="scratchpad"` are de-prioritized."""
        if not keywords:
            return []
        wanted = [k.strip().lower() for k in keywords if k.strip()]
        scored: list[tuple[int, datetime, MemoryItem]] = []
        for item in self._items:
            hay = " ".join(item.keywords + [item.descriptor]).lower()
            hits = sum(1 for w in wanted if w in hay)
            if hits == 0:
                continue
            scratch_penalty = 1 if item.kind == "scratchpad" else 0
            scored.append((hits - scratch_penalty, item.created_at, item))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [it for _, _, it in scored[:limit]]

    def recall_for_goal(self, goal: Goal, *, limit: int = 20) -> list[MemoryItem]:
        return self.recall(_keywords_from_text(goal.text), limit=limit)

    def recall_for_query(self, query: str, *, limit: int = 20) -> list[MemoryItem]:
        return self.recall(_keywords_from_text(query), limit=limit)

    # -- write -------------------------------------------------------

    def add(self, item: MemoryItem) -> None:
        self._items.append(item)
        self._persist()

    def supersede(self, item: MemoryItem, *, key: str) -> None:
        """Add `item` and drop any prior MemoryItem whose `value[key]`
        identifies the same slot (e.g. supersede an old
        `favorite_city`). Lets a later run overwrite a stale fact
        without unbounded growth."""
        new_value = item.value.get(key)
        if new_value is None:
            self.add(item)
            return
        self._items = [
            it
            for it in self._items
            if not (it.kind == item.kind and it.value.get(key) == new_value)
        ]
        self.add(item)

    # -- remember (LLM classifier) ----------------------------------

    REMEMBER_SYSTEM_PROMPT = (
        "You are a memory-extraction classifier for an agent. Given the user's "
        "message, decide whether it contains any DURABLE fact or preference the "
        "agent should remember across runs. A durable fact is a date, a name, a "
        "relationship, a stated preference, or any other piece of information "
        "the user explicitly tells the agent to remember.\n\n"
        "Return a JSON object matching this schema:\n"
        '{ "items": [ { "kind": "fact"|"preference", '
        '"keywords": [<lowercase strings>], '
        '"descriptor": "<one human-readable sentence>", '
        '"value": <JSON object capturing the structured payload> } ] }\n\n'
        "If the user is only asking a question or issuing a non-declarative "
        "command, return {\"items\": []}. Do not invent facts."
    )

    def remember(
        self, user_query: str, *, run_id: str
    ) -> list[MemoryItem]:
        """LLM-classify the user's message for any durable
        fact/preference and persist what it found. Called once at
        the top of agent6.run(), before the iteration loop.

        Quick path: if the message obviously has no declarative
        content (e.g. starts with "what", "when", "show me", "find"),
        skip the LLM call. Cheap fallback for question-shaped queries."""
        if _looks_purely_interrogative(user_query):
            return []

        llm = self._ensure_llm()
        prompt = (
            f"User message:\n\"\"\"\n{user_query.strip()}\n\"\"\"\n\n"
            "Return only the JSON object."
        )
        # response_format dropped — gateway's jsonschema validator and
        # Gemini disagree on union types vs OpenAPI nullable. We parse
        # the model's JSON text ourselves via _json_from_text below
        # and validate each item through Pydantic.
        resp = llm.chat(
            prompt=prompt,
            system=self.REMEMBER_SYSTEM_PROMPT,
            auto_route="memory",
            temperature=1.0,
            max_tokens=512,
        )
        parsed = resp.get("parsed") or _json_from_text(resp.get("text", ""))
        if not parsed:
            return []
        raw_items = parsed.get("items") or []
        out: list[MemoryItem] = []
        for entry in raw_items:
            try:
                item = MemoryItem(
                    id=new_memory_id(),
                    kind=entry["kind"],
                    keywords=[k.lower() for k in entry.get("keywords", [])],
                    descriptor=entry["descriptor"],
                    value=entry.get("value") or {},
                    artifact_id=None,
                    source="user_statement",
                    run_id=run_id,
                    goal_id=None,
                    confidence=0.95,
                    created_at=datetime.now().astimezone(),
                )
            except Exception as exc:
                log.warning("memory.remember: skipped malformed item (%s): %r", exc, entry)
                continue
            self.add(item)
            out.append(item)
        return out

    # -- persistence -------------------------------------------------

    def _load(self) -> list[MemoryItem]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("memory: %s — starting empty", exc)
            return []
        if raw.get("version") != MEMORY_FILE_VERSION:
            log.warning(
                "memory: file version %r != %r — starting empty",
                raw.get("version"),
                MEMORY_FILE_VERSION,
            )
            return []
        out: list[MemoryItem] = []
        for entry in raw.get("items", []):
            try:
                out.append(MemoryItem.model_validate(entry))
            except Exception as exc:
                log.warning("memory: skipped invalid item (%s)", exc)
        return out

    def _persist(self) -> None:
        body = {
            "version": MEMORY_FILE_VERSION,
            "items": [it.model_dump(mode="json") for it in self._items],
        }
        _atomic_write_text(self.path, json.dumps(body, indent=2, default=str))

    def _ensure_llm(self) -> LLM:
        if self._llm is None:
            self._llm = LLM()
        return self._llm


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


_INTERROGATIVE_PREFIX = re.compile(
    r"^\s*(what|when|where|why|who|how|which|tell me|show me|find|search|"
    r"give me|list|fetch|read|convert|compute)\b",
    re.IGNORECASE,
)


def _looks_purely_interrogative(query: str) -> bool:
    """Cheap pre-filter: skip the remember() LLM call for queries
    that are clearly questions or imperative tool tasks rather than
    declarative facts. Saves an LLM call on Query A/B/D-style inputs.
    A query that mixes declarative + interrogative ("remember X then
    tell me Y") will not match — it falls through to the LLM."""
    if not query.strip():
        return True
    first = _INTERROGATIVE_PREFIX.match(query)
    if first is None:
        return False
    # Mixed-mode check: look for "remember", "save", "note that", etc.
    # anywhere in the message → fall through to the LLM.
    if re.search(
        r"\b(remember|note that|save|record|don't forget|please remember)\b",
        query,
        re.IGNORECASE,
    ):
        return False
    return True


_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def _keywords_from_text(text: str) -> list[str]:
    """Extract candidate keywords from free-form text. Filters out
    stopwords and tokens shorter than 3 characters."""
    raw = _WORD_RE.findall(text.lower())
    return [w for w in raw if len(w) >= 3 and w not in _STOPWORDS]


_STOPWORDS = frozenset(
    """
    the and for but not are was were has have had can will may might
    that this these those with from into onto over under your you mine
    one two three about above below before after when where what who
    why how which than then them there their they thee them ourselves
    each every some any all both either neither nor yet such only such
    please thank give me show tell find list also too very still just
    """.split()
)


def _content_extension(content_type: str) -> str:
    """Pick a file extension that makes the artifact human-inspectable."""
    if "markdown" in content_type:
        return ".md"
    if "html" in content_type:
        return ".html"
    if content_type.startswith("text/"):
        return ".txt"
    if "json" in content_type:
        return ".json"
    return ".bin"


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _json_from_text(text: str) -> dict[str, Any] | None:
    """Try to parse the model's text output as JSON. Used as a
    fallback when `resp["parsed"]` is empty (some providers don't
    fill it when response_format isn't natively supported).

    Plain string ops only — no regex on LLM output. The `re` import
    at the top of this module is used on the *user's input message*
    (interrogative prefix check + keyword tokenization), not on
    anything coming back from the model."""
    if not text:
        return None
    text = text.strip()
    # Strip code fences with simple string operations.
    if text.startswith("```"):
        nl = text.find("\n")
        text = text[nl + 1 :] if nl != -1 else text[3:]
        text = text.rstrip()
        if text.endswith("```"):
            text = text[:-3].rstrip()
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None
