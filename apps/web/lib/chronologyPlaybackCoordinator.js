// One authoritative operation, with React consuming immutable projections.
export const TILE_READY_STATES = new Set(["ready", "empty", "error", "unsupported", "timeout"]);

export function coordinatorBarrierComplete(coordinator) {
  return !coordinator?.expectedTileIds?.length || coordinator.expectedTileIds.every(
    (id) => TILE_READY_STATES.has(String(coordinator.tileStates?.[id]?.state || ""))
  );
}

export function createPlaybackCoordinator({
  onResume = () => {}, now = Date.now, schedule = setTimeout, cancelTimer = clearTimeout,
  softTimeoutMs = 3500, hardTimeoutMs = 7000,
} = {}) {
  let snapshot = null;
  let serial = 0;
  let disposed = false;
  const listeners = new Set();
  const timers = { soft: null, hard: null };
  const publish = (next) => {
    snapshot = next;
    for (const listener of listeners) listener();
  };
  const clearTimers = () => {
    for (const key of ["soft", "hard"]) {
      if (timers[key] !== null) cancelTimer(timers[key]);
      timers[key] = null;
    }
  };
  const holding = (id) => !disposed && snapshot?.id === id && snapshot.releaseState === "holding";
  const release = (id, releaseState = "released") => {
    if (!holding(id)) return false;
    clearTimers();
    const resume = snapshot.shouldResume;
    publish({ ...snapshot, releaseState, state: releaseState === "partial" ? "partial" : "ready" });
    if (resume) onResume();
    return true;
  };
  const timeout = (id, timeoutState = "timeout") => {
    if (!holding(id)) return;
    const tileStates = { ...snapshot.tileStates };
    for (const tileId of snapshot.expectedTileIds) {
      if (!TILE_READY_STATES.has(String(tileStates[tileId]?.state || ""))) {
        tileStates[tileId] = { ...(tileStates[tileId] || {}), state: timeoutState,
          facts: { ...(tileStates[tileId]?.facts || {}), timeout: true }, updatedAt: now() };
      }
    }
    publish({ ...snapshot, tileStates, state: "partial" });
    release(id, "partial");
  };
  const start = ({ targetTs, shouldResume, expectedTileIds }) => {
    if (disposed) return null;
    clearTimers();
    const id = `chronology-${now()}-${++serial}`;
    const ids = Array.from(new Set(expectedTileIds.map(String).filter(Boolean)));
    publish({ id, targetTs, shouldResume: Boolean(shouldResume), expectedTileIds: ids,
      tileStates: Object.fromEntries(ids.map((tileId) => [tileId, { state: "pending", updatedAt: now(), facts: {} }])),
      state: "preparing", releaseState: "holding", softTimeoutMs, hardTimeoutMs, createdAt: now() });
    if (!ids.length) release(id);
    else {
      timers.soft = schedule(() => {
        if (holding(id) && !coordinatorBarrierComplete(snapshot)) publish({ ...snapshot, state: "partial" });
      }, softTimeoutMs);
      timers.hard = schedule(() => timeout(id), hardTimeoutMs);
    }
    return snapshot;
  };
  const cancel = (reason = "cancelled") => {
    clearTimers();
    serial += 1;
    if (snapshot?.releaseState === "holding") publish({ ...snapshot, state: reason, releaseState: "cancelled" });
  };
  const updateTile = (tileId, operationId, state, facts = {}) => {
    const id = String(tileId || "");
    if (disposed || !operationId || snapshot?.id !== operationId || snapshot.releaseState === "cancelled"
        || !snapshot.expectedTileIds.includes(id)) return false;
    publish({ ...snapshot, tileStates: { ...snapshot.tileStates,
      [id]: { state, facts, updatedAt: now() } } });
    if (holding(operationId) && coordinatorBarrierComplete(snapshot)) release(operationId);
    return true;
  };
  return {
    start, cancel, release, timeout, updateTile, clearTimers,
    getSnapshot: () => snapshot,
    subscribe: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
    activate: () => { disposed = false; },
    dispose: () => { disposed = true; clearTimers(); listeners.clear(); },
  };
}
