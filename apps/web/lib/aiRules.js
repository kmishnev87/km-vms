export const RULE_KINDS = ["presence", "enter", "exit", "crossing", "dwell", "scene"];
export const RULE_CLASSES = ["person", "vehicle", "animal"];
export const DAYS = [1, 2, 3, 4, 5, 6, 7];
export const EMPTY_RULES = { rules: [], exclusions: [], reference: null };
export const aiRuleCount = (config) => (config?.rules?.length ?? 0) + (config?.exclusions?.length ?? 0);
export const needsPolygon = (kind) => ["enter", "exit", "dwell"].includes(kind);
export const defaultRule = (id) => ({
  id, name: "", kind: "presence", classes: ["person"], points: [], direction: "both",
  scene_objects: false,
  confidence: 50, confirm_seconds: 0, release_seconds: 2, dwell_seconds: 10, schedule: null, record: true, notify: true,
});
export function polygonValid(points) {
  if (points.length < 3 || new Set(points.map(p => p.join(":"))).size !== points.length) return false;
  const edges = points.map((p, i) => [p, points[(i+1) % points.length]]);
  const orient = (a, b, c) => (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0]);
  const hit = (a, b, c, d) => [0, 1].every(i => Math.max(Math.min(a[i], b[i]), Math.min(c[i], d[i])) <=
    Math.min(Math.max(a[i], b[i]), Math.max(c[i], d[i]))) &&
      orient(a, b, c)*orient(a, b, d) <= 0 && orient(c, d, a)*orient(c, d, b) <= 0;
  return Math.abs(edges.reduce((n, [[x, y], [u, v]]) => n + x*v-y*u, 0)) >= 800 &&
    !edges.some(([a, b], i) => edges.some(([c, d], j) => j >= i+2 && (i > 0 || j < edges.length-1) && hit(a, b, c, d)));
}

export function ruleError(config) {
  if (!config) return "";
  if (!config.rules.length) return "rule_add_prompt";
  if ((config.exclusions.length || config.rules.some(rule => rule.points.length)) && !config.reference) return "rule_capture_first";
  if (config.exclusions.some(points => !polygonValid(points))) return "rule_polygon_invalid";
  for (const rule of config.rules) {
    if ((rule.kind !== "scene" || rule.scene_objects) && !rule.classes.length) return "rule_class_required";
    if (rule.schedule && !rule.schedule.days.length) return "rule_days_required";
    if (rule.kind === "crossing") {
      const [a, b] = rule.points;
      if (!a || !b || (a[0]-b[0])**2 + (a[1]-b[1])**2 < 2500) return "rule_line_invalid";
    } else if ((rule.points.length || needsPolygon(rule.kind)) && !polygonValid(rule.points)) return "rule_polygon_invalid";
    if (![[rule.confidence, 35, 95], [rule.confirm_seconds, 0, 20], [rule.release_seconds, 0, 15],
        [rule.dwell_seconds, 3, 300]].every(([v, min, max]) => Number.isInteger(v) && v >= min && v <= max)) return "rule_range";
  }
  return "";
}
export const timeLabel = (n) => [Math.floor(n/60), n%60].map(v => String(v).padStart(2, "0")).join(":");
export const timeMinute = (s) => s.split(":").map(Number).reduce((hours, minutes) => hours*60 + minutes);
