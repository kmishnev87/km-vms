// One owner for overlapping dialogs, including the position-frozen video viewer.
const leases = new Map();
let saved = null;
let wasFrozen = false;
const bodyKeys = ["overflow", "position", "top", "left", "right", "width", "overscrollBehavior"];

export function acquireModalScrollLock({ freezePosition = false } = {}) {
  if (typeof document === "undefined" || typeof window === "undefined") return () => {};
  const body = document.body;
  const root = document.documentElement;
  if (!leases.size) {
    saved = {
      body: Object.fromEntries(bodyKeys.map((key) => [key, body.style[key]])),
      rootOverflow: root.style.overflow,
      rootOverscroll: root.style.overscrollBehavior,
      x: window.scrollX || 0, y: window.scrollY || 0,
    };
    wasFrozen = false;
  }
  const token = Symbol("modal-scroll");
  leases.set(token, freezePosition);
  function synchronize() {
    const frozen = [...leases.values()].some(Boolean);
    for (const key of bodyKeys) body.style[key] = saved.body[key];
    root.style.overflow = saved.rootOverflow;
    root.style.overscrollBehavior = saved.rootOverscroll;
    if (leases.size) {
      body.style.overflow = "hidden";
      body.style.overscrollBehavior = "none";
    }
    if (frozen) {
      root.style.overflow = "hidden";
      root.style.overscrollBehavior = "none";
      Object.assign(body.style, { position: "fixed", top: `-${saved.y}px`, left: `-${saved.x}px`, right: "0", width: "100%" });
    } else if (wasFrozen) {
      window.scrollTo(saved.x, saved.y);
    }
    wasFrozen = frozen;
    if (!leases.size) saved = null;
  }
  synchronize();
  return () => {
    if (!leases.delete(token)) return;
    synchronize();
  };
}
