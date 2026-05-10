export const DEFAULT_PRODUCT_TIMEZONE = "UTC";

export function normalizeProductTimezone(timezone) {
  return String(timezone || "").trim() || DEFAULT_PRODUCT_TIMEZONE;
}

export function formatProductDateTime(value, timezone = DEFAULT_PRODUCT_TIMEZONE, locale = "ru-RU") {
  if (!value) return "";
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  try {
    return new Intl.DateTimeFormat(locale, {
      timeZone: normalizeProductTimezone(timezone),
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(date);
  } catch {
    return new Intl.DateTimeFormat(locale, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(date);
  }
}

export function productDateTimeInputValue(value) {
  return String(value || "").slice(0, 19);
}

export function productLocalInputToApi(value) {
  return productDateTimeInputValue(value);
}

export function productDateFilterParam(dateValue) {
  return String(dateValue || "").slice(0, 10);
}
