"""Side-effect module: adds `mcp-server/` to `sys.path` so the
agent can import `llm_gatewayV3.client` regardless of where Python
was launched from.

Imported (for its side effect) by every cognitive layer that calls
the gateway. Doing this in one tiny module instead of repeating the
sys.path dance in memory.py / perception.py / decision.py keeps the
top of those files clean.
"""
from __future__ import annotations

import sys
from pathlib import Path

_GATEWAY_PARENT = Path(__file__).resolve().parent / "mcp-server"
_path_str = str(_GATEWAY_PARENT)
if _GATEWAY_PARENT.exists() and _path_str not in sys.path:
    sys.path.insert(0, _path_str)
