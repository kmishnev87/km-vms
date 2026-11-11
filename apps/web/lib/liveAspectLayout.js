// Pixel-based packing of complete frames. Playback, ordering and saved stream choices are untouched.
const DEFAULT_RATIO = 16 / 9;
const finite = value => typeof value === "number" && Number.isFinite(value);

export function savedFrameRatio(camera, stream = "main") {
  const size = camera?.stream_dimensions?.[stream];
  const width = Number(size?.width), height = Number(size?.height);
  return finite(width) && finite(height) && width > 0 && height > 0
    && width / height >= 1 / 64 && width / height <= 64 ? width / height : DEFAULT_RATIO;
}

function partitions(count) {
  const result = [];
  if (count <= 12) {
    function visit(remaining, groups) {
      if (!remaining) { result.push(groups); return; }
      for (let size = remaining; size >= 1; size -= 1) visit(remaining - size, [...groups, size]);
    }
    visit(count, []);
  } else {
    // Bounded for larger workspaces: balanced row/column counts and their offsets.
    for (let groups = 1; groups <= count; groups += 1) {
      const base = Math.floor(count / groups), extra = count % groups;
      for (let offset = 0; offset < Math.min(groups, 8); offset += 1) {
        result.push(Array.from({ length: groups }, (_, index) => base + (((index + offset) % groups) < extra ? 1 : 0)));
      }
    }
  }
  return result;
}

function pack(ratios, counts, width, height, header, border, gap, columns, justified) {
  let index = 0;
  const groups = counts.map(count => {
    const values = ratios.slice(index, index += count);
    return { values, count };
  });
  const cross = columns ? height : width, along = columns ? width : height;
  const crossExtra = count => columns ? count * (header + 2 * border) : count * 2 * border;
  const alongExtra = columns ? 2 * border : header + 2 * border;
  const extents = groups.map(group => {
    const factors = group.values.map(ratio => justified ? (columns ? 1 / ratio : ratio) : (columns ? 1 / Math.sqrt(ratio) : Math.sqrt(ratio)));
    const perpendicular = group.values.map(ratio => columns ? Math.sqrt(ratio) : 1 / Math.sqrt(ratio));
    const available = cross - crossExtra(group.count) - gap * (group.count - 1);
    return { ...group, capacity: available / factors.reduce((a, b) => a + b, 0),
      perpendicular: Math.max(...perpendicular) };
  });
  const availableAlong = along - groups.length * alongExtra - gap * (groups.length - 1);
  const scale = justified
    ? Math.min(1, availableAlong / extents.reduce((sum, group) => sum + group.capacity, 0))
    : Math.min(...extents.map(group => group.capacity),
      availableAlong / extents.reduce((sum, group) => sum + group.perpendicular, 0));
  if (!finite(scale) || scale <= 0 || extents.some(group => !finite(group.capacity) || group.capacity <= 0)) return null;
  const groupSizes = extents.map(group => justified ? scale * group.capacity : scale * group.perpendicular);
  const occupied = groupSizes.reduce((sum, size) => sum + size + alongExtra, 0) + gap * (groups.length - 1);
  let alongPosition = (along - occupied) / 2;
  const rectangles = [], areas = [];
  for (let g = 0; g < extents.length; g += 1) {
    const group = extents[g];
    const sizes = group.values.map(ratio => {
      const videoWidth = justified ? (columns ? groupSizes[g] : groupSizes[g] * ratio) : scale * Math.sqrt(ratio);
      const videoHeight = justified ? (columns ? groupSizes[g] / ratio : groupSizes[g]) : scale / Math.sqrt(ratio);
      return { width: videoWidth + 2 * border, height: videoHeight + header + 2 * border, area: videoWidth * videoHeight };
    });
    const occupiedCross = sizes.reduce((sum, size) => sum + (columns ? size.height : size.width), 0) + gap * (group.count - 1);
    let crossPosition = (cross - occupiedCross) / 2;
    for (const size of sizes) {
      const crossSize = columns ? size.height : size.width, alongSize = columns ? size.width : size.height;
      const centredAlong = alongPosition + (groupSizes[g] + alongExtra - alongSize) / 2;
      rectangles.push({ x: columns ? centredAlong : crossPosition, y: columns ? crossPosition : centredAlong,
        width: size.width, height: size.height });
      areas.push(size.area);
      crossPosition += crossSize + gap;
    }
    alongPosition += groupSizes[g] + alongExtra + gap;
  }
  const minimum = Math.min(...areas), maximum = Math.max(...areas);
  // Avoid a huge single frame surrounded by tiny ones merely to inflate total area.
  if (minimum <= 0 || (justified && maximum / minimum > 2.6)) return null;
  if (rectangles.some(r => r.x < -.001 || r.y < -.001 || r.x + r.width > width + .001 || r.y + r.height > height + .001)) return null;
  return { rectangles, score: areas.reduce((a, b) => a + b, 0) * Math.pow(minimum / maximum, .1) };
}

function allowToolbarWidth(rectangles, width, minimumWidth, gap) {
  const result = rectangles.map(rectangle => ({ ...rectangle }));
  if (!finite(minimumWidth) || minimumWidth <= 0) return result;
  for (const r of result) {
    if (r.width >= minimumWidth) continue;
    let left = 0, right = width;
    for (const other of result) {
      if (other === r || other.y + other.height <= r.y + .001 || r.y + r.height <= other.y + .001) continue;
      if (other.x + other.width <= r.x + .001) left = Math.max(left, other.x + other.width + gap);
      else if (other.x >= r.x + r.width - .001) right = Math.min(right, other.x - gap);
    }
    const nextWidth = Math.max(r.width, Math.min(minimumWidth, right - left));
    r.x = Math.max(left, Math.min(right - nextWidth, r.x - (nextWidth - r.width) / 2));
    r.width = nextWidth;
  }
  return result;
}

export function fitCameraFrames(tiles, cameras, bounds, { headerHeight = 34, border = 1, gap = 6, minimumWidth = 0 } = {}) {
  const width = Number(bounds?.width), height = Number(bounds?.height);
  if (!Array.isArray(tiles) || !tiles.length || !finite(width) || !finite(height) || width <= 0 || height <= 0) return tiles;
  const byId = new Map((Array.isArray(cameras) ? cameras : []).map(camera => [String(camera.id), camera]));
  const ratios = tiles.map(tile => savedFrameRatio(byId.get(String(tile.cameraId)), tile.stream));
  const bar = finite(headerHeight) && headerHeight >= 0 ? headerHeight : 34;
  const edge = finite(border) && border >= 0 ? border : 1;
  const space = finite(gap) && gap >= 0 ? gap : 6;
  let best = null;
  for (const counts of partitions(tiles.length)) {
    for (const columns of [false, true]) for (const justified of [false, true]) {
      const candidate = pack(ratios, counts, width, height, bar, edge, space, columns, justified);
      if (candidate && (!best || candidate.score > best.score + 1)) best = candidate;
    }
  }
  if (!best) return tiles;
  const rectangles = allowToolbarWidth(best.rectangles, width, minimumWidth, space);
  return tiles.map((tile, index) => {
    const r = rectangles[index];
    return { ...tile, xPct: Math.max(0, r.x / width), yPct: Math.max(0, r.y / height),
      wPct: r.width / width, hPct: r.height / height };
  });
}

export function sameFrameLayout(a, b) {
  return a.length === b.length && a.every((tile, index) => tile.id === b[index].id
    && ["xPct", "yPct", "wPct", "hPct"].every(key => Math.abs(tile[key] - b[index][key]) < .000001));
}
