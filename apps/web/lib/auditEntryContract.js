export const AUDIT_CATEGORIES = [
  "auth",
  "users",
  "settings",
  "cameras",
  "live",
  "records",
  "chronology",
  "archive",
  "security",
  "diagnostics",
  "system",
  "recorder",
  "storage",
  "retention",
  "reconciliation",
];

export const AUDIT_SEVERITIES = ["info", "warning", "error", "security"];
export const AUDIT_LIMIT = 50;

export const AUDIT_FILTER_KEYS = [
  "category",
  "severity",
  "since_minutes",
  "period",
  "actor",
  "target",
  "target_type",
  "target_id",
  "event_type",
  "q",
];

const STRING_FILTERS = new Set(["actor", "target", "target_type", "target_id", "event_type", "q"]);
const SENSITIVE_FILTER_VALUE = /(rtsp:\/\/|authorization|bearer\s+|password|token=|access_token|media_token|secret|credential)/i;
const PERIOD_TO_MINUTES = {
  "1h": "60",
  "6h": "360",
  "24h": "1440",
  "all": "",
};

function boundedText(value, maxLength = 120) {
  const text = String(value || "").trim();
  if (!text || text.length > maxLength) return "";
  if (SENSITIVE_FILTER_VALUE.test(text)) return "";
  return text;
}

function sanitizeSinceMinutes(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || String(parsed) !== raw) return "";
  if (parsed < 1 || parsed > 60 * 24 * 30) return "";
  return String(parsed);
}

export function defaultAuditFilters() {
  return {
    category: "",
    severity: "",
    since_minutes: "1440",
    actor: "",
    target: "",
    target_type: "",
    target_id: "",
    event_type: "",
    q: "",
  };
}

export function sanitizeAuditFiltersFromEntries(entries) {
  const filters = defaultAuditFilters();
  const unsupported = [];
  const invalid = [];

  for (const [key, rawValue] of entries) {
    if (!AUDIT_FILTER_KEYS.includes(key)) {
      unsupported.push(key);
      continue;
    }
    if (key === "period") {
      const period = String(rawValue || "").trim().toLowerCase();
      if (Object.prototype.hasOwnProperty.call(PERIOD_TO_MINUTES, period)) {
        filters.since_minutes = PERIOD_TO_MINUTES[period];
      } else {
        invalid.push(key);
      }
      continue;
    }
    if (key === "category") {
      const value = boundedText(rawValue, 80);
      if (AUDIT_CATEGORIES.includes(value)) filters.category = value;
      else if (value) invalid.push(key);
      continue;
    }
    if (key === "severity") {
      const value = boundedText(rawValue, 40);
      if (AUDIT_SEVERITIES.includes(value)) filters.severity = value;
      else if (value) invalid.push(key);
      continue;
    }
    if (key === "since_minutes") {
      const value = sanitizeSinceMinutes(rawValue);
      if (value) filters.since_minutes = value;
      else if (String(rawValue || "").trim()) invalid.push(key);
      continue;
    }
    if (STRING_FILTERS.has(key)) {
      const value = boundedText(rawValue, key === "event_type" ? 160 : 120);
      if (value) filters[key] = value;
      else if (String(rawValue || "").trim()) invalid.push(key);
    }
  }

  return {
    filters,
    unsupported: [...new Set(unsupported)].sort(),
    invalid: [...new Set(invalid)].sort(),
  };
}

export function sanitizeAuditFiltersFromSearchParams(searchParams) {
  if (!searchParams) return { filters: defaultAuditFilters(), unsupported: [], invalid: [] };
  return sanitizeAuditFiltersFromEntries(Array.from(searchParams.entries()));
}

export function buildAuditEventsPath(filters, offset = 0) {
  const safe = sanitizeAuditFiltersFromEntries(Object.entries(filters || {})).filters;
  const params = new URLSearchParams();
  params.set("limit", String(AUDIT_LIMIT));
  params.set("offset", String(Math.max(0, Number.parseInt(String(offset || 0), 10) || 0)));
  for (const key of ["category", "severity", "actor", "target", "target_type", "target_id", "event_type", "q"]) {
    if (safe[key]) params.set(key, safe[key]);
  }
  if (safe.since_minutes) params.set("since_minutes", safe.since_minutes);
  return `/audit/events?${params.toString()}`;
}
