export function installModalKeyboard(container, onClose, doc = document) {
  const previous = doc.activeElement;
  const focusables = () => Array.from(container.querySelectorAll(
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), video[controls], [tabindex]:not([tabindex="-1"])'
  )).filter(element => element.getClientRects().length && !element.closest('[hidden], [inert]'));
  const focusFirst = () => (focusables()[0] || container).focus({ preventScroll: true });
  function keydown(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      const fullscreen = doc.fullscreenElement;
      if (fullscreen && container.contains(fullscreen)) {
        doc.exitFullscreen?.().catch(() => {});
      } else onClose();
    } else if (event.key === "Tab") {
      const items = focusables();
      const first = items[0] || container;
      const last = items.at(-1) || container;
      if (!container.contains(doc.activeElement) || (event.shiftKey ? doc.activeElement === first : doc.activeElement === last) || !items.length) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus({ preventScroll: true });
      }
    }
  }
  function focusin(event) { if (!container.contains(event.target)) focusFirst(); }
  doc.addEventListener("keydown", keydown, true);
  doc.addEventListener("focusin", focusin, true);
  focusFirst();
  return () => {
    doc.removeEventListener("keydown", keydown, true);
    doc.removeEventListener("focusin", focusin, true);
    if (previous?.isConnected) previous.focus({ preventScroll: true });
  };
}
