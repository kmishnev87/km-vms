const TERMINAL_STATUSES = new Set([
  "completed", "failed", "failed_rolled_back", "blocked",
  "cancelled", "canceled",
]);

export function updateApplyOperationKey(operation) {
  return String(operation?.request_id || operation?.submission_id || "").trim();
}

export function updateApplyOperationIsTerminal(operation) {
  return TERMINAL_STATUSES.has(String(operation?.effective_status || operation?.status || "").toLowerCase());
}

export function updateApplySameOperation(left, right) {
  const key = updateApplyOperationKey(left);
  return Boolean(key && key === updateApplyOperationKey(right));
}

// A history record may supply missing details, never those of a different attempt.
export function updateApplyOperationView(status, { allowHistory = false } = {}) {
  const current = status || {};
  const summary = current.last_apply_summary;
  const idle = !current.status || current.status === "idle";
  if (idle && !updateApplyOperationKey(current) && allowHistory
    && updateApplyOperationKey(summary) && updateApplyOperationIsTerminal(summary)) {
    return { ...summary, effective_status: summary.status, last_apply_summary: summary };
  }
  if (!updateApplySameOperation(current, summary)) return current;
  return {
    ...current,
    started_at: current.started_at || summary.started_at,
    finished_at: current.finished_at || summary.finished_at,
    steps: current.steps?.length ? current.steps : summary.steps || [],
  };
}

// Keep the displayed attempt during a transient empty status, not across a new one.
export function retainUpdateApplyOperation(previous, incoming) {
  const next = updateApplyOperationView(incoming, { allowHistory: true });
  if (!previous || !updateApplyOperationKey(previous)) return next;
  const incomingKey = updateApplyOperationKey(incoming);
  const nextKey = updateApplyOperationKey(next);
  if (!incomingKey && (!incoming?.status || incoming.status === "idle")) {
    if (!updateApplyOperationIsTerminal(previous) && nextKey === updateApplyOperationKey(previous)
      && updateApplyOperationIsTerminal(next)) return next;
    return updateApplyOperationIsTerminal(previous) ? previous : {
      ...previous, status: "unknown", effective_status: "unknown",
    };
  }
  if (updateApplySameOperation(previous, next) && updateApplyOperationIsTerminal(previous)
    && !updateApplyOperationIsTerminal(next)) return previous;
  return next;
}
