export function maintenanceStatusText(status, t) {
  const key = status || "unknown";
  const labels = t.maintenanceStatuses || {};
  return labels[key] || labels.unknown || t.maintenanceStatusUnknown || "Unknown";
}
export function boundedFiniteNumber(value, fallback, min, max) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, parsed));
}

export function boundedContractText(value, maxLength = 160) {
  return String(value || "").trim().slice(0, maxLength);
}

export function normalizeMaintenanceBackendText(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const lower = raw.toLowerCase();
  if (lower === "schema metadata is already valid.") return "schema_metadata_valid";
  if (lower === "schema metadata is already valid") return "schema_metadata_valid";
  if (lower === "schema is current; no pending migrations.") return "schema_current_no_pending_migrations";
  if (lower === "schema is current; no pending migrations") return "schema_current_no_pending_migrations";
  if (lower === "database schema preparation failed during update apply.") return "schema_update_failed";
  if (lower === "review the database schema preparation failure before retrying the update.") return "schema_update_retry_after_cause_resolved";
  if (lower === "the preserved previous release no longer matches the current installation.") return "slot_adoption_conflict";
  if (lower === "verify the installed source and runtime state before retrying.") return "slot_adoption_conflict_action";
  if (lower === "no valid restore artifacts are available in configured backup root.") return "restore_no_valid_artifacts";
  if (lower === "no valid restore artifacts are available in the configured backup root.") return "restore_no_valid_artifacts";
  if (lower.includes("no durable maintenance action history is available")) return "maintenance_history_limited";
  if (/^[a-z0-9_:-]+$/.test(lower)) return lower.replaceAll(":", "_").replaceAll("-", "_");
  return lower;
}

export function formatMaintenanceMessage(value, t, lang = "ru", context = "status") {
  const key = normalizeMaintenanceBackendText(value);
  const labels = t.maintenanceMessageLabels || {};
  if (key && labels[key]) return labels[key];
  if (key && t.maintenanceStatuses?.[key]) return t.maintenanceStatuses[key];
  if (context === "action" || context === "blocker" || context === "error") {
    return t.maintenanceActionFallback || t.maintenanceMessageFallback || maintenanceStatusText("unknown", t);
  }
  return t.maintenanceMessageFallback || maintenanceStatusText("unknown", t);
}
