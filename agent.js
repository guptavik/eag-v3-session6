// Manual agent loop. Calls the LLM, executes any tool_use blocks,
// feeds results back, and stops when the model returns end_turn or
// the safety cap is hit.
//
// Callbacks let the UI render the reasoning chain live:
//   onStep(step)        -> called once when a tool starts (status: "loading")
//                       -> called again with status "retrying" if first attempt fails
//                       -> called once with "success" | "error" + result
//   onAssistantText(s)  -> any text the model emits between tool calls
//   onFinalText(s)      -> the model's final user-facing message
//   onError(err)        -> fatal error (network, auth, unknown tool, etc.)

const MAX_ITERATIONS = 10;

// Run a tool, retry once on failure. Caller surfaces both attempts via onStep.
async function executeWithRetry(toolName, toolInput, onRetry) {
  try {
    return { ok: true, result: await executeTool(toolName, toolInput) };
  } catch (err1) {
    const msg1 = err1 && err1.message ? err1.message : String(err1);
    if (onRetry) onRetry(msg1);
    try {
      return { ok: true, result: await executeTool(toolName, toolInput), retried: true };
    } catch (err2) {
      const msg2 = err2 && err2.message ? err2.message : String(err2);
      return { ok: false, error: msg2, firstError: msg1, retried: true };
    }
  }
}

async function runAgent(userQuery, callbacks = {}) {
  const {
    onStep = () => {},
    onAssistantText = () => {},
    onFinalText = () => {},
    onError = () => {}
  } = callbacks;

  const conversationHistory = [
    { role: "user", content: userQuery }
  ];

  // Detect user TZ once per run and pass to the LLM each turn so the
  // final brief renders meeting times in the user's local zone with
  // the correct abbreviation (e.g. "2:00 PM CST"), not UTC.
  let userTimeZone = null;
  try {
    userTimeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch {
    // Intl is universally available in Chrome; the catch is purely defensive.
  }

  let stepNumber = 0;

  try {
    for (let iter = 0; iter < MAX_ITERATIONS; iter++) {
      const response = await callLLM(conversationHistory, TOOLS, { userTimeZone });

      // Surface any text content the model produced this turn.
      const textBlocks = response.content.filter(b => b.type === "text");
      for (const tb of textBlocks) {
        if (tb.text && tb.text.trim()) onAssistantText(tb.text);
      }

      // Always append the assistant's full content to history before
      // sending tool results — the API requires every tool_use block to
      // be paired with a matching tool_result on the next user turn.
      conversationHistory.push({ role: "assistant", content: response.content });

      if (response.stop_reason !== "tool_use") {
        // Done. Surface the final text (the same content we already streamed
        // above is the canonical answer).
        const finalText = textBlocks.map(t => t.text).join("\n").trim();
        onFinalText(finalText);
        return { conversationHistory, stopReason: response.stop_reason };
      }

      const toolUseBlocks = response.content.filter(b => b.type === "tool_use");

      // Execute each tool sequentially and collect results in one user message.
      const toolResults = [];
      for (const block of toolUseBlocks) {
        stepNumber++;
        const stepId = stepNumber;
        onStep({
          stepId,
          status: "loading",
          toolName: block.name,
          toolInput: block.input
        });

        const outcome = await executeWithRetry(block.name, block.input, (firstErrMsg) => {
          onStep({
            stepId,
            status: "retrying",
            toolName: block.name,
            toolInput: block.input,
            error: firstErrMsg
          });
        });

        if (outcome.ok) {
          onStep({
            stepId,
            status: "success",
            toolName: block.name,
            toolInput: block.input,
            result: outcome.result,
            retried: outcome.retried || false
          });
          toolResults.push({
            type: "tool_result",
            tool_use_id: block.id,
            content: JSON.stringify(outcome.result)
          });
        } else {
          onStep({
            stepId,
            status: "error",
            toolName: block.name,
            toolInput: block.input,
            error: outcome.error,
            firstError: outcome.firstError,
            retried: true
          });
          // Return the error to the model as a tool_result so it can adapt
          // (try a different query, skip the step, etc.) rather than aborting.
          toolResults.push({
            type: "tool_result",
            tool_use_id: block.id,
            is_error: true,
            content: `Tool error after one retry: ${outcome.error}`
          });
        }
      }

      conversationHistory.push({ role: "user", content: toolResults });
    }

    // Hit the iteration cap without end_turn.
    const capMsg = `Stopped after ${MAX_ITERATIONS} iterations without a final answer. The agent may be stuck in a loop.`;
    onError(new Error(capMsg));
    return { conversationHistory, stopReason: "max_iterations" };
  } catch (err) {
    onError(err);
    throw err;
  }
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { runAgent, MAX_ITERATIONS };
}
