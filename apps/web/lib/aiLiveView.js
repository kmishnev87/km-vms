export const LIVE_VIEW_WIDTH = 20;
export const LIVE_VIEW_HEIGHT = 12;

export function aiLiveCopy(locale) {
  if (locale === "ru") return "Линии и зоны AI";
  if (locale === "zh-CN") return "AI 线条和区域";
  return "AI lines and zones";
}

export function aiLiveStatusCopy(locale, state) {
  const copy = {
    waiting: ["Ждём кадр", "Waiting for a frame", "等待画面"],
    uncalibrated: ["Нужно обновить кадр в настройках AI", "Refresh the frame in AI settings", "请在 AI 设置中刷新画面"],
    mismatch: ["Не удалось подтвердить ракурс", "The camera view could not be verified", "无法确认摄像机视角"],
    profile: ["Тип объектива не подтверждён", "The camera profile could not be verified", "无法确认摄像机配置"],
  };
  return copy[state]?.[locale === "ru" ? 0 : locale === "zh-CN" ? 2 : 1] || "";
}

export function readViewReference(view) {
  if (!view || !Number.isInteger(view.ratio) || view.ratio < 50 || view.ratio > 20000) return null;
  try {
    const edges = atob(view.edges.replace(/-/g, "+").replace(/_/g, "/"));
    const mode = ["fixed", "unknown"].includes(view.mode) ? view.mode : "ptz";
    return edges.length === 112 ? { ratio: view.ratio, edges, mode } : null;
  } catch { return null; }
}

export function liveGeometryMatches(reference, width, height) {
  return Boolean(reference && Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0 &&
    Math.abs(width / height * 1000 - reference.ratio) <= Math.max(16, reference.ratio * .018));
}

// Same 20×12 calibration sketch as Recorder. Only this display check tolerates
// changed contrast and differently scaled streams. It never changes AI analysis.
export function viewMatchScore(reference, luma, width, height) {
  if (!reference || luma.length !== 240 || !Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return 0;
  const difference = Math.abs(width / height * 1000 - reference.ratio);
  const sameAspect = difference <= Math.max(16, reference.ratio * .018);
  if (difference > reference.ratio * .30) return 0;

  function compare(dx, dy) {
    let index = 0, known = 0, light = 0, dark = 0, matched = 0, extra = 0, blank = 0;
    const quadrants = [0, 0, 0, 0];
    function edge(x, y, vertical) {
      const dir = (reference.edges.charCodeAt(index >> 2) >> ((index++ & 3) * 2)) & 3;
      const sx = x + dx, sy = y + dy;
      if (sx < 0 || sy < 0 || sx >= 20 - Number(!vertical) || sy >= 12 - Number(vertical)) return;
      const i = sy * 20 + sx, delta = luma[i + (vertical ? 20 : 1)] - luma[i];
      const strong = Math.abs(delta) >= 8;
      if (!dir) { blank++; extra += Number(strong); return; }
      known++;
      const signed = dir === 1 ? delta : -delta;
      light += Number(signed >= 6);
      dark += Number(signed <= -6);
      if (strong) {
        matched++;
        quadrants[Number(x >= 10) + 2 * Number(y >= 6)]++;
      }
    }
    for (let y = 0; y < 12; y++) for (let x = 0; x < 19; x++) edge(x, y, false);
    for (let y = 0; y < 11; y++) for (let x = 0; x < 20; x++) edge(x, y, true);
    return { known, light: light / known, dark: dark / known, coverage: matched / known,
      structure: matched / known - (blank ? extra / blank : 1),
      spread: quadrants.filter(n => n >= 4).length };
  }
  const still = compare(0, 0);
  if (still.known < 24) return 0;
  if (sameAspect && still.light >= .77) return still.light;
  // IR can reverse contrast (or only some edges). Do not accept simple
  // horizons, repeated patterns or busy textures that match just as well
  // shifted: landmarks must agree on their location, not just their presence.
  const polarity = sameAspect ? still.dark : Math.max(still.light, still.dark);
  const signedCandidate = polarity >= .86;
  const structureCandidate = still.coverage >= .70 && still.structure >= .22;
  if (still.spread >= 3 && (signedCandidate || structureCandidate)) {
    let shiftedSigned = 0, shiftedStructure = -1;
    for (let y = -1; y <= 1; y++) for (let x = -1; x <= 1; x++) if (x || y) {
      const shifted = compare(x, y);
      if (shifted.known >= 24) {
        shiftedSigned = Math.max(shiftedSigned, shifted.light, shifted.dark);
        shiftedStructure = Math.max(shiftedStructure, shifted.structure);
      }
    }
    if (signedCandidate && polarity >= shiftedSigned + .12 ||
        structureCandidate && still.structure >= shiftedStructure + .12) return .8;
  }
  return sameAspect ? still.light : 0;
}

export function viewStability(previous, score) {
  // Hide promptly on a clearly different view; require consecutive matching
  // frames before restoring. Ordinary foreground motion does not flash lines.
  if (score >= .77) {
    const good = Math.min(3, previous.good + 1);
    return { good, bad: 0, visible: previous.visible || good >= 3 };
  }
  const bad = Math.min(2, previous.bad + 1);
  return { good: 0, bad, visible: previous.visible && score >= .48 && bad < 2 };
}
