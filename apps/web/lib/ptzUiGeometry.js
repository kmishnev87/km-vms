export function ptzSpeedLevel(clientX, left, width) {
  const fraction = (Number(clientX) - Number(left) - 8) / Math.max(1, Number(width) - 16);
  return Math.max(1, Math.min(5, Math.round(fraction * 4) + 1));
}

export function clampPtzPanel(position, bounds, panel, padding = 12) {
  return {
    x: Math.max(padding, Math.min(Number(position.x) || 0, Math.max(padding, bounds.width - panel.width - padding))),
    y: Math.max(padding, Math.min(Number(position.y) || 0, Math.max(padding, bounds.height - panel.height - padding))),
  };
}

export function fullscreenPtzPosition(position, bounds, panel, viewport, nativeActive = false) {
  if (!position) return null;
  // Mount/exit may briefly expose the small inline tile. Never turn that
  // transitional clamp into the user's saved fullscreen position.
  const fullWindow = bounds.width >= viewport.width - 32 && bounds.height >= viewport.height - 96;
  return nativeActive || fullWindow ? clampPtzPanel(position, bounds, panel) : position;
}

export function ptzImagePoint(clientX, clientY, bounds, dimensions) {
  if (!(bounds.width > 0 && bounds.height > 0 && dimensions.width > 0 && dimensions.height > 0)) return null;
  const scale = Math.min(bounds.width / dimensions.width, bounds.height / dimensions.height);
  const width = dimensions.width * scale, height = dimensions.height * scale;
  const x = (clientX - bounds.left - (bounds.width - width) / 2) / width;
  const y = (clientY - bounds.top - (bounds.height - height) / 2) / height;
  return x >= 0 && x <= 1 && y >= 0 && y <= 1 ? { x, y } : null;
}

export function ptzImageBounds(bounds, dimensions) {
  if (![bounds.left, bounds.top, bounds.width, bounds.height, dimensions.width, dimensions.height].every(Number.isFinite)
      || Math.min(bounds.width, bounds.height, dimensions.width, dimensions.height) <= 0) return null;
  const scale = Math.min(bounds.width / dimensions.width, bounds.height / dimensions.height);
  const width = dimensions.width * scale, height = dimensions.height * scale;
  return { left: bounds.left + (bounds.width - width) / 2, top: bounds.top + (bounds.height - height) / 2, width, height };
}

export function ptzImageRectangle(start, end, bounds, dimensions) {
  const image = ptzImageBounds(bounds, dimensions);
  if (!image || ![start.x, start.y, end.x, end.y].every(Number.isFinite)
      || !ptzImagePoint(start.x, start.y, bounds, dimensions)) return null;
  const endX = Math.max(image.left, Math.min(image.left + image.width, end.x));
  const endY = Math.max(image.top, Math.min(image.top + image.height, end.y));
  if (Math.abs(endX - start.x) < 8 || Math.abs(endY - start.y) < 8) return null;
  return { left: (Math.min(start.x, endX) - image.left) / image.width,
    top: (Math.min(start.y, endY) - image.top) / image.height,
    right: (Math.max(start.x, endX) - image.left) / image.width,
    bottom: (Math.max(start.y, endY) - image.top) / image.height };
}
