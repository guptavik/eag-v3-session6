// UI controller for the Meeting Intelligence Agent popup.
// Wires the API key form, query input, and reasoning/brief panels
// to the agent loop in agent.js.

(function () {
  // ---------- DOM refs ----------
  const apiKeySection = document.getElementById("api-key-section");
  const gearBtn       = document.getElementById("gear-btn");
  const gearStatus    = document.getElementById("gear-status");
  const apiKeyInput   = document.getElementById("api-key");
  const saveKeyBtn    = document.getElementById("save-key-btn");
  const keyStatus     = document.getElementById("key-status");

  const customQuery = document.getElementById("custom-query");
  const runBtn      = document.getElementById("run-btn");
  const quickBtns   = document.querySelectorAll(".quick-btn");

  const reasoningSection = document.getElementById("reasoning-section");
  const reasoningToggle  = document.getElementById("reasoning-toggle");
  const reasoningMeta    = document.getElementById("reasoning-meta");
  const copyBtn          = document.getElementById("copy-btn");
  const stepsContainer   = document.getElementById("steps");

  const briefSection = document.getElementById("brief-section");
  const briefEl      = document.getElementById("brief");

  const errorSection = document.getElementById("error-section");
  const errorEl      = document.getElementById("error");

  // Track step DOM nodes by stepId so we can update them in place.
  const stepNodes = new Map();
  let running = false;

  // Snapshot of the current run, used by the Copy button to serialize
  // query + reasoning chain + final brief as markdown.
  let runHistory = null;

  // ---------- Init: load saved key ----------
  (async function init() {
    try {
      const { geminiApiKey } = await chrome.storage.local.get("geminiApiKey");
      if (geminiApiKey) {
        apiKeyInput.value = geminiApiKey;
        showKeyStatus("Key loaded from storage.", "success");
        setKeyStatusIndicator("saved");
      } else {
        setKeyStatusIndicator("unset");
        // No key on first open — pop the settings so the user sees the field.
        openSettings();
      }
    } catch (err) {
      showKeyStatus(`Failed to load key: ${err.message}`, "error");
      setKeyStatusIndicator("error");
    }
  })();

  // ---------- Event wiring ----------
  saveKeyBtn.addEventListener("click", onSaveKey);
  runBtn.addEventListener("click", () => onRun(customQuery.value.trim()));
  customQuery.addEventListener("keydown", e => {
    if (e.key === "Enter") onRun(customQuery.value.trim());
  });
  for (const btn of quickBtns) {
    btn.addEventListener("click", () => onRun(btn.dataset.query));
  }
  reasoningToggle.addEventListener("click", () => {
    reasoningSection.classList.toggle("collapsed");
  });
  reasoningToggle.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      reasoningSection.classList.toggle("collapsed");
    }
  });
  copyBtn.addEventListener("click", e => {
    e.stopPropagation();   // don't also toggle the section
    onCopy();
  });
  gearBtn.addEventListener("click", e => {
    e.stopPropagation();
    toggleSettings();
  });
  // Close on click outside the popover or gear button.
  document.addEventListener("mousedown", e => {
    if (apiKeySection.classList.contains("hidden")) return;
    if (apiKeySection.contains(e.target) || gearBtn.contains(e.target)) return;
    closeSettings();
  });
  // Close on Escape when the popover is open.
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !apiKeySection.classList.contains("hidden")) {
      closeSettings();
      gearBtn.focus();
    }
  });

  // ---------- Handlers ----------
  async function onSaveKey() {
    const key = apiKeyInput.value.trim();
    if (!key) {
      showKeyStatus("Enter a key first.", "error");
      return;
    }
    try {
      await setApiKey(key);
      showKeyStatus("Key saved.", "success");
      setKeyStatusIndicator("saved");
      closeSettings();
    } catch (err) {
      showKeyStatus(`Save failed: ${err.message}`, "error");
      setKeyStatusIndicator("error");
    }
  }

  async function onRun(query) {
    if (running) return;
    if (!query) {
      showError("Enter a query (or click a quick action).");
      return;
    }
    if (!apiKeyInput.value.trim()) {
      showError("Save your Gemini API key first.");
      return;
    }

    resetOutput();
    runHistory = {
      query,
      timestamp: new Date().toISOString(),
      steps: [],
      finalText: ""
    };
    setRunning(true);
    reasoningSection.classList.remove("hidden");

    try {
      // Lazy-load the MCP tool registry on first run so the popup opens
      // cleanly even when the MCP server is down — the user sees the
      // failure inline with their query rather than at popup open.
      if (TOOLS.length === 0) {
        await fetchToolDefinitions();
      }
      await runAgent(query, {
        onStep: handleStep,
        onAssistantText: handleAssistantText,
        onFinalText: handleFinalText,
        onError: handleError
      });
    } catch (err) {
      // runAgent re-throws after calling onError, so this is mostly a safety net.
      handleError(err);
    } finally {
      setRunning(false);
    }
  }

  // ---------- Reasoning chain rendering ----------
  function handleStep(step) {
    let node = stepNodes.get(step.stepId);
    if (!node) {
      node = createStepNode(step);
      stepNodes.set(step.stepId, node);
      stepsContainer.appendChild(node);
    }
    updateStepNode(node, step);
    node.scrollIntoView({ behavior: "smooth", block: "nearest" });

    // Mirror the step into runHistory so Copy can serialize the run.
    if (runHistory) {
      let tracked = runHistory.steps.find(s => s.stepId === step.stepId);
      if (!tracked) {
        tracked = { stepId: step.stepId };
        runHistory.steps.push(tracked);
      }
      Object.assign(tracked, step);
    }
  }

  function createStepNode(step) {
    const wrapper = document.createElement("div");
    // Default-collapsed: only the header is visible until the user opens it
    // (or until an error auto-expands it).
    wrapper.className = "step collapsed";

    const header = document.createElement("div");
    header.className = "step-header";
    header.innerHTML = `
      <span class="step-icon"></span>
      <span class="step-num">Step ${step.stepId}</span>
      <span class="step-tool"></span>
      <span class="step-summary"></span>
      <span class="step-chevron">▾</span>
    `;
    header.addEventListener("click", () => wrapper.classList.toggle("collapsed"));

    const body = document.createElement("div");
    body.className = "step-body";

    wrapper.appendChild(header);
    wrapper.appendChild(body);
    return wrapper;
  }

  function updateStepNode(node, step) {
    // Always collapse: the status icon (✓ / ❌ / ⏳) plus the summary text
    // give enough at-a-glance signal. The user clicks to drill in.
    node.className = `step step-status-${step.status} collapsed`;
    updateReasoningMeta();

    const iconEl    = node.querySelector(".step-icon");
    const toolEl    = node.querySelector(".step-tool");
    const summaryEl = node.querySelector(".step-summary");
    const body      = node.querySelector(".step-body");

    iconEl.textContent = iconFor(step.status);
    toolEl.textContent = `${step.toolName}()`;
    summaryEl.textContent = summaryFor(step);

    body.innerHTML = "";

    body.appendChild(makeSection("Input", JSON.stringify(step.toolInput || {}, null, 2)));

    if (step.status === "success") {
      body.appendChild(renderToolResult(step.toolName, step.result));
      if (step.retried) {
        body.appendChild(makeNote("Recovered after one retry."));
      }
    } else if (step.status === "retrying") {
      body.appendChild(makeNote(`First attempt failed: ${step.error}. Retrying...`));
    } else if (step.status === "error") {
      const detail = step.firstError && step.firstError !== step.error
        ? `${step.error}\n\nFirst attempt: ${step.firstError}`
        : step.error;
      body.appendChild(makeSection("Error", detail, true));
    }
    // For "loading", body just shows Input.
  }

  function updateReasoningMeta() {
    const total = stepNodes.size;
    if (!total) {
      reasoningMeta.textContent = "";
      return;
    }
    let errors = 0;
    let inFlight = 0;
    for (const node of stepNodes.values()) {
      if (node.classList.contains("step-status-error")) errors++;
      else if (
        node.classList.contains("step-status-loading") ||
        node.classList.contains("step-status-retrying")
      ) inFlight++;
    }
    const parts = [`${total} step${total === 1 ? "" : "s"}`];
    if (inFlight) parts.push(`${inFlight} running`);
    if (errors)   parts.push(`${errors} error${errors === 1 ? "" : "s"}`);
    reasoningMeta.textContent = parts.join(" · ");
  }

  function iconFor(status) {
    switch (status) {
      case "loading":  return "⏳";
      case "retrying": return "🔄";
      case "success":  return "✓";
      case "error":    return "❌";
      default:         return "•";
    }
  }

  function summaryFor(step) {
    switch (step.status) {
      case "loading":  return "running";
      case "retrying": return "retrying";
      case "success":  return summarizeResult(step.result);
      case "error":    return "failed";
      default:         return "";
    }
  }

  function summarizeResult(result) {
    if (Array.isArray(result)) return `${result.length} item${result.length === 1 ? "" : "s"}`;
    if (result && typeof result === "object") {
      if (typeof result.totalMeetings === "number") {
        const parts = [`${result.totalMeetings} meeting${result.totalMeetings === 1 ? "" : "s"}`];
        if (result.totalHours)  parts.push(`${result.totalHours} hrs`);
        if (result.busiestDay)  parts.push(`busiest ${result.busiestDay.slice(0, 3)}`);
        return parts.join(" · ");
      }
      if (result.name)  return result.name;
      if (result.title) return result.title;
    }
    return "ok";
  }

  // Per-tool result renderers. Default falls back to a JSON dump.
  function renderToolResult(toolName, result) {
    if (toolName === "calculateMeetingStats" && result && typeof result === "object") {
      return renderMeetingStatsCard(result);
    }
    return makeSection("Result", JSON.stringify(result, null, 2));
  }

  function renderMeetingStatsCard(stats) {
    const wrapper = document.createElement("div");
    wrapper.className = "stats-card";

    // 2x2 metric tiles
    const tiles = [
      ["Total Meetings", stats.totalMeetings ?? 0, ""],
      ["Total Hours",    stats.totalHours    ?? 0, "hrs"],
      ["Avg Duration",   stats.averageDurationHours ?? 0, "hrs"],
      ["Busiest Day",    stats.busiestDay || "—", ""]
    ];
    const grid = document.createElement("div");
    grid.className = "stats-grid";
    for (const [label, value, unit] of tiles) {
      const tile = document.createElement("div");
      tile.className = "stat-tile";
      const labelEl = document.createElement("div");
      labelEl.className = "stat-label";
      labelEl.textContent = label;
      const valueEl = document.createElement("div");
      valueEl.className = "stat-value";
      valueEl.textContent = String(value);
      if (unit) {
        const unitEl = document.createElement("span");
        unitEl.className = "stat-unit";
        unitEl.textContent = ` ${unit}`;
        valueEl.appendChild(unitEl);
      }
      tile.appendChild(labelEl);
      tile.appendChild(valueEl);
      grid.appendChild(tile);
    }
    wrapper.appendChild(grid);

    // Weekly load chart (hours per day, color-coded by load classification).
    // Show all weekdays even when free; show weekends only if they have meetings.
    const dayOrder = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];
    const hoursByDay = stats.hoursByDay || {};
    const loadByDay  = stats.loadByDay  || {};
    const meetingsByDay = stats.meetingsByDay || {};

    const visibleDays = dayOrder.filter(d => {
      if (d === "Saturday" || d === "Sunday") return (hoursByDay[d] || 0) > 0;
      return true;
    });

    if (visibleDays.length) {
      const heading = document.createElement("div");
      heading.className = "stats-chart-label";
      heading.textContent = "Weekly Load";
      wrapper.appendChild(heading);

      const chart = document.createElement("div");
      chart.className = "load-chart";
      const maxHours = Math.max(...visibleDays.map(d => hoursByDay[d] || 0), 1);

      for (const day of visibleDays) {
        const hours = hoursByDay[day] || 0;
        const load  = loadByDay[day] || "free";
        const pct   = (hours / maxHours) * 100;

        const row = document.createElement("div");
        row.className = `load-row load-row--${load}`;

        const label = document.createElement("span");
        label.className = "load-label";
        label.textContent = day.slice(0, 3);

        const bar = document.createElement("div");
        bar.className = "load-bar";
        const fill = document.createElement("div");
        fill.className = `load-fill load-fill--${load}`;
        fill.style.width = `${pct.toFixed(1)}%`;
        bar.appendChild(fill);

        const value = document.createElement("span");
        value.className = "load-value";
        value.textContent = hours > 0 ? formatHours(hours) : "—";

        const badge = document.createElement("span");
        badge.className = `load-badge load-badge--${load}`;
        badge.textContent = load;

        row.appendChild(label);
        row.appendChild(bar);
        row.appendChild(value);
        row.appendChild(badge);
        chart.appendChild(row);
      }
      wrapper.appendChild(chart);
    }

    // Day-by-day breakdown — collapsible per day, listing each meeting.
    const daysWithMeetings = dayOrder.filter(d => (meetingsByDay[d] || []).length > 0);
    if (daysWithMeetings.length) {
      const heading = document.createElement("div");
      heading.className = "stats-chart-label";
      heading.textContent = "Day-by-Day Breakdown";
      wrapper.appendChild(heading);

      const breakdown = document.createElement("div");
      breakdown.className = "day-breakdown";
      for (const day of daysWithMeetings) {
        breakdown.appendChild(buildDayBlock(day, meetingsByDay[day], hoursByDay[day] || 0));
      }
      wrapper.appendChild(breakdown);
    }

    if (stats.timeframe && stats.timeframe !== "n/a") {
      const tf = document.createElement("div");
      tf.className = "stats-timeframe";
      tf.textContent = `Timeframe: ${stats.timeframe}`;
      wrapper.appendChild(tf);
    }

    return wrapper;
  }

  function buildDayBlock(day, meetings, hours) {
    const block = document.createElement("div");
    block.className = "day-block collapsed";

    const header = document.createElement("div");
    header.className = "day-header";
    header.setAttribute("role", "button");
    header.setAttribute("tabindex", "0");

    const name = document.createElement("span");
    name.className = "day-name";
    name.textContent = day;

    const summary = document.createElement("span");
    summary.className = "day-summary";
    summary.textContent =
      `${meetings.length} meeting${meetings.length === 1 ? "" : "s"} · ${formatHours(hours)}`;

    const chevron = document.createElement("span");
    chevron.className = "day-chevron";
    chevron.textContent = "▾";

    header.appendChild(name);
    header.appendChild(summary);
    header.appendChild(chevron);

    const body = document.createElement("div");
    body.className = "day-body";
    const list = document.createElement("ul");
    list.className = "day-meetings";
    for (const m of meetings) {
      const li = document.createElement("li");
      li.className = "day-meeting";

      const time = document.createElement("span");
      time.className = "day-meeting-time";
      time.textContent = formatTimeRange(m.startTime, m.durationMinutes);

      const title = document.createElement("span");
      title.className = "day-meeting-title";
      title.textContent = m.title;

      li.appendChild(time);
      li.appendChild(title);

      if (m.location) {
        const loc = document.createElement("span");
        loc.className = "day-meeting-loc";
        loc.textContent = m.location;
        li.appendChild(loc);
      }

      list.appendChild(li);
    }
    body.appendChild(list);

    block.appendChild(header);
    block.appendChild(body);

    const toggle = () => block.classList.toggle("collapsed");
    header.addEventListener("click", toggle);
    header.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });

    return block;
  }

  function formatHours(h) {
    if (!h) return "0 hr";
    const rounded = +h.toFixed(2);
    return `${rounded} hr${rounded === 1 ? "" : "s"}`;
  }

  function formatTimeRange(startIso, durationMinutes) {
    try {
      const start = new Date(startIso);
      const time = start.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      return `${time} · ${durationMinutes}m`;
    } catch {
      return `${durationMinutes}m`;
    }
  }

  function makeSection(label, content, isError) {
    const wrapper = document.createElement("div");
    wrapper.className = isError ? "step-section step-error" : "step-section";

    const labelEl = document.createElement("div");
    labelEl.className = "step-section-label";
    labelEl.textContent = label;

    const pre = document.createElement("pre");
    pre.textContent = content;

    wrapper.appendChild(labelEl);
    wrapper.appendChild(pre);
    return wrapper;
  }

  function makeNote(text) {
    const note = document.createElement("div");
    note.className = "step-section-label";
    note.textContent = text;
    return note;
  }

  // Tagged reasoning blocks emitted by the SYSTEM_PROMPT's
  // "Reasoning transparency rules" section. Each tag = a distinct
  // reasoning type so the UI can color/icon them differently and the
  // user can scan the agent's chain of thought at a glance.
  const REASONING_TAGS = {
    LOOKUP:     { icon: "🔎", label: "LOOKUP",     cls: "lookup" },
    SYNTHESIS:  { icon: "✍️", label: "SYNTHESIS",  cls: "synthesis" },
    SCHEDULING: { icon: "📅", label: "SCHEDULING", cls: "scheduling" },
    SEARCH:     { icon: "🌐", label: "SEARCH",     cls: "search" },
    PROFILE:    { icon: "👤", label: "PROFILE",    cls: "profile" }
  };

  // A single text part from the model can contain multiple tagged blocks
  // (e.g. "... [LOOKUP] ... [PROFILE] ..."). Split on the [TAG] markers
  // and render each block as its own row; untagged prose falls back to
  // the legacy "thought" treatment.
  function handleAssistantText(text) {
    const blocks = splitTaggedBlocks(text);
    for (const block of blocks) {
      stepsContainer.appendChild(renderReasoningBlock(block));
    }
  }

  function splitTaggedBlocks(text) {
    const tagRegex = /\[(LOOKUP|SYNTHESIS|SCHEDULING|SEARCH|PROFILE)\]/g;
    const indexes = [];
    let match;
    while ((match = tagRegex.exec(text)) !== null) {
      indexes.push({ tag: match[1], start: match.index, contentStart: match.index + match[0].length });
    }
    if (indexes.length === 0) {
      return [{ tag: null, content: text.trim() }];
    }
    const blocks = [];
    // Any prose before the first tag becomes an untagged block.
    if (indexes[0].start > 0) {
      const prefix = text.slice(0, indexes[0].start).trim();
      if (prefix) blocks.push({ tag: null, content: prefix });
    }
    for (let i = 0; i < indexes.length; i++) {
      const end = i + 1 < indexes.length ? indexes[i + 1].start : text.length;
      const content = text.slice(indexes[i].contentStart, end).trim();
      blocks.push({ tag: indexes[i].tag, content });
    }
    return blocks;
  }

  function renderReasoningBlock({ tag, content }) {
    const meta = tag ? REASONING_TAGS[tag] : null;
    const node = document.createElement("div");
    node.className = meta
      ? `assistant-text reasoning-block reasoning-block--${meta.cls} collapsed`
      : "assistant-text collapsed";

    const header = document.createElement("div");
    header.className = "assistant-text-header";

    const preview = content.replace(/\s+/g, " ").trim();
    const truncated = preview.length > 80 ? preview.slice(0, 80) + "…" : preview;

    const iconText = meta ? meta.icon : "💭";
    header.innerHTML = `
      <span class="assistant-text-icon"></span>
      ${meta ? `<span class="reasoning-tag">${meta.label}</span>` : ""}
      <span class="assistant-text-preview"></span>
      <span class="step-chevron">▾</span>
    `;
    header.querySelector(".assistant-text-icon").textContent = iconText;
    header.querySelector(".assistant-text-preview").textContent = truncated;

    const body = document.createElement("div");
    body.className = "assistant-text-body";
    body.textContent = content;

    node.appendChild(header);
    node.appendChild(body);
    header.addEventListener("click", () => node.classList.toggle("collapsed"));

    return node;
  }

  function handleFinalText(text) {
    if (!text) return;
    briefSection.classList.remove("hidden");
    briefEl.innerHTML = renderMarkdown(text);
    postProcessBrief(briefEl);
    if (runHistory) runHistory.finalText = text;
  }

  // ---------- Brief post-processing ----------
  // The markdown renderer produces flat HTML (h1, p, h2, ul, etc.).
  //   - Single h1 → render as hero card with sections below.
  //   - Multiple h1s → wrap each h1+content in a collapsible meeting card.
  //   - h2 sections within either form become typed blocks (attendees,
  //     emails, talking-points, checklist, etc.).
  function postProcessBrief(root) {
    const children = [...root.children];
    const groups = [];
    const preamble = [];
    let group = null;

    for (const el of children) {
      if (el.tagName === "H1") {
        group = { h1: el, content: [] };
        groups.push(group);
      } else if (group) {
        group.content.push(el);
      } else {
        preamble.push(el);
      }
    }

    root.innerHTML = "";
    for (const el of preamble) root.appendChild(el);

    if (groups.length <= 1) {
      // Zero or one meeting: keep flat structure (with hero if present)
      if (groups.length === 1) {
        root.appendChild(groups[0].h1);
        for (const el of groups[0].content) root.appendChild(el);
      }
      groupBlocks(root);
      if (groups.length === 1) decorateHero(root);
    } else {
      // Multiple meetings: collapsible cards, all collapsed by default
      for (const g of groups) root.appendChild(buildMeetingCard(g));
    }
  }

  // Wrap every h2 + its following content (until the next h2) into a
  // typed .brief-block container, then apply per-block transforms.
  function groupBlocks(scope) {
    const children = [...scope.children];
    const next = [];
    let block = null;

    for (const el of children) {
      if (el.tagName === "H2") {
        const type = blockType(el.textContent);
        block = document.createElement("div");
        block.className = `brief-block brief-block--${type}`;
        block.dataset.type = type;
        block.appendChild(el);
        next.push(block);
      } else if (block) {
        block.appendChild(el);
      } else {
        next.push(el);
      }
    }

    scope.innerHTML = "";
    for (const c of next) scope.appendChild(c);

    for (const blk of scope.querySelectorAll(".brief-block")) {
      transformBlock(blk, blk.dataset.type);
    }
  }

  function buildMeetingCard(group) {
    const card = document.createElement("div");
    card.className = "meeting-card collapsed";

    const header = document.createElement("div");
    header.className = "meeting-card-header";
    header.setAttribute("role", "button");
    header.setAttribute("tabindex", "0");

    const titleRow = document.createElement("div");
    titleRow.className = "meeting-card-title-row";
    group.h1.classList.add("meeting-card-title");
    titleRow.appendChild(group.h1);

    const chevron = document.createElement("span");
    chevron.className = "meeting-card-chevron";
    chevron.textContent = "▾";
    titleRow.appendChild(chevron);

    header.appendChild(titleRow);

    // Pull the first paragraph (When/Where/Agenda meta) into the header
    if (group.content.length > 0 && group.content[0].tagName === "P") {
      const meta = group.content.shift();
      meta.classList.add("meeting-card-meta");
      header.appendChild(meta);
    }

    card.appendChild(header);

    const body = document.createElement("div");
    body.className = "meeting-card-body";
    for (const el of group.content) body.appendChild(el);
    groupBlocks(body);
    card.appendChild(body);

    const toggle = () => card.classList.toggle("collapsed");
    header.addEventListener("click", toggle);
    header.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });

    return card;
  }

  function blockType(headingText) {
    const t = headingText.toLowerCase();
    if (t.includes("attendee"))                      return "attendees";
    if (t.includes("compan"))                        return "company";
    if (t.includes("email"))                         return "emails";
    if (t.includes("talking") || t.includes("topic")) return "talking-points";
    if (t.includes("prep") || t.includes("checklist")) return "checklist";
    if (t.includes("agenda"))                        return "agenda";
    if (t.includes("risk") || t.includes("gap"))     return "risks";
    return "default";
  }

  function decorateHero(root) {
    const h1 = root.querySelector("h1");
    if (!h1) return;
    const hero = document.createElement("div");
    hero.className = "brief-hero";
    h1.parentNode.insertBefore(hero, h1);
    hero.appendChild(h1);
    // Pull the first paragraph (the When/Where/Agenda meta) into the hero.
    const meta = hero.nextElementSibling;
    if (meta && meta.tagName === "P") {
      meta.classList.add("brief-meta");
      hero.appendChild(meta);
    }
  }

  function transformBlock(block, type) {
    if (type === "attendees") {
      for (const li of block.querySelectorAll("li")) {
        if (li.classList.contains("checklist")) continue;
        li.classList.add("attendee-card");
        const strong = li.querySelector("strong");
        const initial = strong
          ? strong.textContent.trim().charAt(0).toUpperCase()
          : "?";
        const avatar = document.createElement("span");
        avatar.className = "attendee-avatar";
        avatar.textContent = initial;
        const content = document.createElement("span");
        content.className = "attendee-content";
        while (li.firstChild) content.appendChild(li.firstChild);
        li.appendChild(avatar);
        li.appendChild(content);
      }
    } else if (type === "emails") {
      for (const li of block.querySelectorAll("li")) {
        if (li.classList.contains("checklist")) continue;
        li.classList.add("email-card");
      }
    } else if (type === "talking-points") {
      let n = 1;
      for (const li of block.querySelectorAll("li")) {
        if (li.classList.contains("checklist")) continue;
        li.classList.add("talking-point");
        const num = document.createElement("span");
        num.className = "talking-point-num";
        num.textContent = String(n++);
        const content = document.createElement("span");
        content.className = "talking-point-content";
        while (li.firstChild) content.appendChild(li.firstChild);
        li.appendChild(num);
        li.appendChild(content);
      }
    }
    // checklist: existing .checklist class handles its own styling.
  }

  function handleError(err) {
    const message = err && err.message ? err.message : String(err);
    showError(message);
  }

  function showError(message) {
    errorSection.classList.remove("hidden");
    errorEl.textContent = message;
  }

  function resetOutput() {
    stepNodes.clear();
    stepsContainer.innerHTML = "";
    briefEl.innerHTML = "";
    briefSection.classList.add("hidden");
    errorSection.classList.add("hidden");
    errorEl.textContent = "";
    reasoningSection.classList.add("hidden");
  }

  function setRunning(isRunning) {
    running = isRunning;
    runBtn.disabled = isRunning;
    runBtn.textContent = isRunning ? "Running..." : "Go";
    customQuery.disabled = isRunning;
    for (const btn of quickBtns) btn.disabled = isRunning;
  }

  function showKeyStatus(message, kind) {
    keyStatus.textContent = message;
    keyStatus.className = `hint ${kind || ""}`.trim();
  }

  function setKeyStatusIndicator(state) {
    // state ∈ "saved" | "unset" | "error" — drives the gear-button dot color.
    gearStatus.className = `gear-status ${state}`;
  }

  function openSettings() {
    apiKeySection.classList.remove("hidden");
    gearBtn.classList.add("open");
    gearBtn.setAttribute("aria-expanded", "true");
    apiKeyInput.focus();
  }

  function closeSettings() {
    apiKeySection.classList.add("hidden");
    gearBtn.classList.remove("open");
    gearBtn.setAttribute("aria-expanded", "false");
  }

  function toggleSettings() {
    if (apiKeySection.classList.contains("hidden")) openSettings();
    else closeSettings();
  }

  // ---------- Copy full run ----------
  async function onCopy() {
    const text = buildCopyText();
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      flashCopyButton("Copied", "copied");
    } catch (err) {
      flashCopyButton("Copy failed", "error");
      console.error("Copy failed:", err);
    }
  }

  function flashCopyButton(label, cls) {
    const original = copyBtn.textContent;
    const originalCls = copyBtn.className;
    copyBtn.textContent = label;
    copyBtn.classList.add(cls);
    setTimeout(() => {
      copyBtn.textContent = original;
      copyBtn.className = originalCls;
    }, 1800);
  }

  function buildCopyText() {
    if (!runHistory) return "";
    const lines = [];
    lines.push("# Meeting Intelligence Agent — Run Output");
    lines.push("");
    lines.push(`**Query:** ${runHistory.query}`);
    lines.push(`**Time:** ${runHistory.timestamp}`);
    lines.push("");

    if (runHistory.steps.length) {
      lines.push("## Reasoning Chain");
      lines.push("");
      for (const step of runHistory.steps) {
        const icon = stepIconForCopy(step.status);
        const retryNote = step.retried ? " _(recovered after retry)_" : "";
        lines.push(`### Step ${step.stepId}: \`${step.toolName}\` ${icon}${retryNote}`);
        lines.push("");
        lines.push("**Input:**");
        lines.push("```json");
        lines.push(JSON.stringify(step.toolInput || {}, null, 2));
        lines.push("```");
        lines.push("");
        if (step.status === "success") {
          lines.push("**Result:**");
          lines.push("```json");
          lines.push(JSON.stringify(step.result, null, 2));
          lines.push("```");
        } else if (step.status === "error") {
          lines.push(`**Error:** ${step.error || "(unknown)"}`);
          if (step.firstError && step.firstError !== step.error) {
            lines.push(`First attempt: ${step.firstError}`);
          }
        } else {
          lines.push(`_Status: ${step.status}_`);
        }
        lines.push("");
      }
    }

    if (runHistory.finalText) {
      lines.push("---");
      lines.push("");
      lines.push("## Final Brief");
      lines.push("");
      lines.push(runHistory.finalText);
    }

    return lines.join("\n").trim() + "\n";
  }

  function stepIconForCopy(status) {
    switch (status) {
      case "success":  return "✓";
      case "error":    return "✗";
      case "retrying": return "↻";
      case "loading":  return "…";
      default:         return "•";
    }
  }

  // ---------- Minimal Markdown renderer ----------
  // Handles: # / ## / ### headings, **bold**, *italic*, `code`,
  // - bullet lists, - [ ] / - [x] checklists, [text](url) links.
  // All text content is HTML-escaped before any tag substitution.
  function renderMarkdown(src) {
    const lines = src.replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      if (!line.trim()) { i++; continue; }

      // Headings
      let m;
      if ((m = line.match(/^(#{1,3})\s+(.+)$/))) {
        const level = m[1].length;
        out.push(`<h${level}>${inline(m[2])}</h${level}>`);
        i++;
        continue;
      }

      // Table block: header row + separator row + data rows
      // Header:    | col | col |
      // Separator: |---|---| (with optional :---: alignment markers)
      // Data rows: | val | val |
      if (/^\s*\|.*\|\s*$/.test(line)) {
        const sep = lines[i + 1];
        if (sep && /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/.test(sep)) {
          const headers = parseTableRow(line);
          i += 2;
          const rows = [];
          while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
            rows.push(parseTableRow(lines[i]));
            i++;
          }
          out.push(renderTable(headers, rows));
          continue;
        }
      }

      // List block (handles bullets and checklists together)
      if (/^\s*-\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*-\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*-\s+/, ""));
          i++;
        }
        out.push("<ul>");
        for (const item of items) {
          const checkMatch = item.match(/^\[( |x|X)\]\s+(.+)$/);
          if (checkMatch) {
            const checked = checkMatch[1].toLowerCase() === "x";
            const box = checked ? "☑" : "☐";
            out.push(`<li class="checklist">${box} ${inline(checkMatch[2])}</li>`);
          } else {
            out.push(`<li>${inline(item)}</li>`);
          }
        }
        out.push("</ul>");
        continue;
      }

      // Paragraph: gather consecutive non-empty, non-special lines.
      const buf = [];
      while (
        i < lines.length &&
        lines[i].trim() &&
        !/^(#{1,3})\s+/.test(lines[i]) &&
        !/^\s*-\s+/.test(lines[i]) &&
        !/^\s*\|.*\|\s*$/.test(lines[i])
      ) {
        buf.push(lines[i]);
        i++;
      }
      if (buf.length) out.push(`<p>${inline(buf.join(" "))}</p>`);
    }

    return out.join("\n");
  }

  function parseTableRow(line) {
    return line.trim()
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map(c => c.trim());
  }

  function renderTable(headers, rows) {
    const head = headers.map(h => `<th>${inline(h)}</th>`).join("");
    const body = rows
      .map(r => "<tr>" + r.map(c => `<td>${inline(c)}</td>`).join("") + "</tr>")
      .join("");
    return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  function inline(text) {
    let s = escapeHtml(text);
    // Code spans first (so their contents aren't further processed).
    s = s.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
    // Bold then italic. Order matters because **x** would also match *x*.
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    // Links [text](url) — URL is already HTML-escaped by escapeHtml above.
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, url) =>
      `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`
    );
    return s;
  }

  function escapeHtml(s) {
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
})();
