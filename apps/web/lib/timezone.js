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

export function productInstantInputValue(value, timezone = DEFAULT_PRODUCT_TIMEZONE) {
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) return '';
  let parts;
  try {
    parts = new Intl.DateTimeFormat('en-CA', { timeZone: normalizeProductTimezone(timezone),
      year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit',
      hourCycle:'h23' }).formatToParts(date);
  } catch {
    return date.toISOString().slice(0, 19);
  }
  const fields = Object.fromEntries(parts.filter(part => part.type !== 'literal').map(part => [part.type, part.value]));
  return `${fields.year}-${fields.month}-${fields.day}T${fields.hour}:${fields.minute}:${fields.second}`;
}

// A product-calendar coordinate, not an instant. UTC is only the browser-
// independent carrier so browser DST cannot change wall-clock arithmetic.
export function productCalendarDate(value) {
  if (value instanceof Date) return new Date(value.getTime());
  return new Date(`${String(value || '').slice(0, 19)}Z`);
}

export function productCalendarInputValue(value) {
  const date = productCalendarDate(value);
  return Number.isFinite(date.getTime()) ? date.toISOString().slice(0, 19) : '';
}
