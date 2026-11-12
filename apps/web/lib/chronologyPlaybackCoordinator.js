// One authoritative operation, with React consuming immutable projections.
export const TILE_READY_STATES = new Set(["ready", "empty"]);

export function coordinatorBarrierComplete(coordinator) {
  return !coordinator?.expectedTileIds?.length || coordinator.expectedTileIds.every(
    (id) => TILE_READY_STATES.has(String(coordinator.tileStates?.[id]?.state || ""))
  );
}

export function createPlaybackCoordinator({
  onResume = () => {}, onHold = () => {}, onProgress = () => {},
  now = Date.now, schedule = setTimeout, cancelTimer = clearTimeout,
  softTimeoutMs = 3500, hardTimeoutMs = 7000,
} = {}) {
  let snapshot = null;
  let serial = 0;
  let disposed = false;
  let lastPositionMs = null;
  const listeners = new Set();
  const players = new Map();
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
  const pausePlayers = () => { for (const player of players.values()) player.pause?.(); };
  const samples = () => (snapshot?.expectedTileIds || []).flatMap(id => {
    // A newly attached source starts at media time zero before its seek. It
    // must never pull an already playing group back to that file's beginning.
    if (snapshot.tileStates?.[id]?.state !== "ready") return [];
    const value = players.get(id)?.sample?.();
    return value && Number.isFinite(value.positionMs) ? [{ id, ...value }] : [];
  });
  const position = () => {
    const values = samples();
    return values.length ? Math.min(...values.map(value => value.positionMs)) : lastPositionMs ?? snapshot?.targetMs;
  };
  const release = (id, releaseState = "released") => {
    if (!holding(id) || !coordinatorBarrierComplete(snapshot)) return false;
    clearTimers();
    const resume = snapshot.shouldResume;
    publish({ ...snapshot, releaseState, state: releaseState === "partial" ? "partial" : "ready" });
    if (resume) onResume({ empty: snapshot.expectedTileIds.every(tileId => snapshot.tileStates[tileId]?.state === "empty") });
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
    // A deadline changes presentation, never eligibility or the common clock.
    publish({ ...snapshot, tileStates, state: "waiting" });
  };
  const start = ({ targetTs, targetMs, shouldResume, expectedTileIds, reason = "seek" }) => {
    if (disposed) return null;
    clearTimers();
    const id = `chronology-${now()}-${++serial}`;
    const ids = Array.from(new Set(expectedTileIds.map(String).filter(Boolean)));
    if (Number.isFinite(targetMs)) lastPositionMs = targetMs;
    pausePlayers();
    publish({ id, targetTs, targetMs, reason, shouldResume: Boolean(shouldResume), expectedTileIds: ids,
      tileStates: Object.fromEntries(ids.map((tileId) => [tileId, { state: "pending", updatedAt: now(), facts: {} }])),
      state: "preparing", releaseState: "holding", softTimeoutMs, hardTimeoutMs, createdAt: now() });
    onHold(targetMs, reason);
    if (!ids.length) release(id);
    else {
      timers.soft = schedule(() => {
        if (holding(id) && !coordinatorBarrierComplete(snapshot)) publish({ ...snapshot, state: "waiting" });
      }, softTimeoutMs);
      timers.hard = schedule(() => timeout(id), hardTimeoutMs);
    }
    return snapshot;
  };
  const cancel = (reason = "cancelled") => {
    clearTimers();
    serial += 1;
    pausePlayers();
    if (snapshot) publish({ ...snapshot, shouldResume: false, state: reason, releaseState: "cancelled" });
  };
  const hold = (reason = "buffering", targetMs = position()) => {
    if (disposed || !snapshot?.shouldResume || snapshot.releaseState !== "released") return false;
    start({ targetTs: snapshot.targetTs, targetMs, shouldResume: true, expectedTileIds: snapshot.expectedTileIds, reason });
    return true;
  };
  const updateTile = (tileId, operationId, state, facts = {}) => {
    const id = String(tileId || "");
    if (disposed || !operationId || snapshot?.id !== operationId || snapshot.releaseState === "cancelled"
        || !snapshot.expectedTileIds.includes(id)) return false;
    if (state === "progress") {
      if (snapshot.releaseState !== "released" || !snapshot.shouldResume) return true;
      const values = samples();
      if (values.length) {
        const minimum = Math.min(...values.map(value => value.positionMs));
        if (values.some(value => value.positionMs - minimum > 500)) hold("resynchronizing", minimum);
        else { lastPositionMs = minimum; onProgress(minimum); }
      }
      return true;
    }
    if (snapshot.releaseState === "released" && snapshot.shouldResume
        && ["loading", "buffering", "boundary", "error", "unsupported"].includes(state)) {
      hold(state, state === "boundary" && Number.isFinite(facts.targetMs) ? facts.targetMs : position());
      return true;
    }
    publish({ ...snapshot, tileStates: { ...snapshot.tileStates,
      [id]: { state, facts, updatedAt: now() } } });
    if (holding(operationId) && coordinatorBarrierComplete(snapshot)) release(operationId);
    return true;
  };
  const setMembership = (tileIds) => {
    if (!snapshot || disposed) return;
    const ids = Array.from(new Set(tileIds.map(String).filter(Boolean)));
    if (ids.length === snapshot.expectedTileIds.length && ids.every(id => snapshot.expectedTileIds.includes(id))) return;
    if (!ids.length) { cancel("empty_workspace"); return; }
    const added = ids.some(id => !snapshot.expectedTileIds.includes(id));
    publish({ ...snapshot, expectedTileIds: ids,
      tileStates: Object.fromEntries(ids.map(id => [id, snapshot.tileStates[id] || { state: "pending", facts: {}, updatedAt: now() }])) });
    if (added && snapshot.releaseState === "released" && snapshot.shouldResume) hold("membership");
    else if (holding(snapshot.id) && coordinatorBarrierComplete(snapshot)) release(snapshot.id);
  };
  return {
    start, cancel, release, timeout, updateTile, clearTimers,
    setMembership,
    registerTile(id, player) {
      players.set(String(id), player);
      return () => { if (players.get(String(id)) === player) players.delete(String(id)); };
    },
    getSnapshot: () => snapshot,
    subscribe: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
    activate: () => { disposed = false; },
    dispose: () => { disposed = true; pausePlayers(); clearTimers(); listeners.clear(); players.clear(); },
  };
}
