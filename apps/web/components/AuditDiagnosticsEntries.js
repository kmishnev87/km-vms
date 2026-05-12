"use client";

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { apiFetch, apiFetchBlob, forbiddenMessage } from "../lib/api";
import { useCurrentUser } from "../lib/currentUser";
import { useI18n } from "../lib/i18n";
import {
  AUDIT_CATEGORIES,
  AUDIT_LIMIT,
  AUDIT_SEVERITIES,
  buildAuditEventsPath,
  sanitizeAuditFiltersFromSearchParams,
} from "../lib/auditEntryContract";

const AUDIT_LABELS = {
  category: {
    auth: { ru: "Авторизация", en: "Auth", "zh-CN": "授权" },
    users: { ru: "Пользователи", en: "Users", "zh-CN": "用户" },
    settings: { ru: "Настройки", en: "Settings", "zh-CN": "设置" },
    cameras: { ru: "Камеры", en: "Cameras", "zh-CN": "摄像机" },
    live: { ru: "Live", en: "Live", "zh-CN": "实时" },
    records: { ru: "Записи", en: "Records", "zh-CN": "录像" },
    chronology: { ru: "Хронология", en: "Chronology", "zh-CN": "时间轴" },
    archive: { ru: "Архив", en: "Archive", "zh-CN": "归档" },
    security: { ru: "Безопасность", en: "Security", "zh-CN": "安全" },
    diagnostics: { ru: "Диагностика", en: "Diagnostics", "zh-CN": "诊断" },
    system: { ru: "Система", en: "System", "zh-CN": "系统" },
    recorder: { ru: "Recorder", en: "Recorder", "zh-CN": "录像服务" },
    storage: { ru: "Хранилище", en: "Storage", "zh-CN": "存储" },
    retention: { ru: "Retention", en: "Retention", "zh-CN": "保留" },
    reconciliation: { ru: "Reconciliation", en: "Reconciliation", "zh-CN": "一致性检查" },
  },
  severity: {
    info: { ru: "Инфо", en: "Info", "zh-CN": "信息" },
    warning: { ru: "Предупреждение", en: "Warning", "zh-CN": "警告" },
    error: { ru: "Ошибка", en: "Error", "zh-CN": "错误" },
    security: { ru: "Security", en: "Security", "zh-CN": "安全" },
  },
};

function localizedLabel(kind, value, locale) {
  return AUDIT_LABELS[kind]?.[value]?.[locale] || AUDIT_LABELS[kind]?.[value]?.en || value || "";
}

function auditMessage(event, locale) {
  if (locale === "en") return event.message_en || event.message_ru || event.event_type;
  return event.message_ru || event.message_en || event.event_type;
}

function auditTarget(event, t) {
  const parts = [event.target_type, event.target_id, event.target_name].filter(Boolean);
  return parts.length ? parts.join(": ") : t("securityJournal.noTarget");
}

function formatTimestamp(value, locale) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 32);
  return new Intl.DateTimeFormat(locale === "zh-CN" ? "zh-CN" : locale, {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(date);
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename || "km-vms-diagnostics.zip";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export function SecurityJournalEntry() {
  const searchParams = useSearchParams();
  const { currentUser, loading } = useCurrentUser();
  const { locale, t } = useI18n();
  const parsed = useMemo(() => sanitizeAuditFiltersFromSearchParams(searchParams), [searchParams]);
  const [filters, setFilters] = useState(parsed.filters);
  const [items, setItems] = useState([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const canReadAudit = Boolean(currentUser?.permissions?.includes("manage_settings"));

  useEffect(() => {
    setFilters(parsed.filters);
    setOffset(0);
  }, [parsed]);

  const queryPath = useMemo(() => buildAuditEventsPath(filters, 0), [filters]);

  useEffect(() => {
    if (!canReadAudit) return;
    loadEvents(0);
  }, [canReadAudit, queryPath]);

  function patchFilter(key, value) {
    setOffset(0);
    setFilters((current) => ({ ...current, [key]: value }));
  }

  async function loadEvents(nextOffset = 0) {
    setBusy(true);
    setError("");
    try {
      const data = await apiFetch(buildAuditEventsPath(filters, nextOffset));
      const nextItems = Array.isArray(data?.items) ? data.items : [];
      setItems(nextOffset > 0 ? (current) => [...current, ...nextItems] : nextItems);
      setOffset(nextOffset + nextItems.length);
      setHasMore(nextItems.length === AUDIT_LIMIT);
    } catch (err) {
      if (nextOffset === 0) setItems([]);
      setError(String(err?.message || t("securityJournal.error")));
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return <div className="settingsJournalEmpty">{t("common.loading")}</div>;
  }
  if (!canReadAudit) {
    return <div className="settingsJournalEmpty error">{forbiddenMessage(locale)}</div>;
  }

  return (
    <section className="settingsSecurityModalSection">
      <div className="settingsSecurityModalSectionHead">
        <h2>{t("securityJournal.title")}</h2>
        <span>{t("securityJournal.subtitle")}</span>
      </div>

      {(parsed.unsupported.length || parsed.invalid.length) ? (
        <div className="settingsJournalEmpty warning">
          {t("securityJournal.unsupportedFilters")}
        </div>
      ) : null}

      <div className="settingsAuditFilters" aria-label={t("securityJournal.filters")}>
        <label>
          <span>{t("securityJournal.category")}</span>
          <select className="select" value={filters.category} onChange={(event) => patchFilter("category", event.target.value)}>
            <option value="">{t("securityJournal.all")}</option>
            {AUDIT_CATEGORIES.map((category) => (
              <option key={category} value={category}>{localizedLabel("category", category, locale)}</option>
            ))}
          </select>
        </label>
        <label>
          <span>{t("securityJournal.severity")}</span>
          <select className="select" value={filters.severity} onChange={(event) => patchFilter("severity", event.target.value)}>
            <option value="">{t("securityJournal.all")}</option>
            {AUDIT_SEVERITIES.map((severity) => (
              <option key={severity} value={severity}>{localizedLabel("severity", severity, locale)}</option>
            ))}
          </select>
        </label>
        <label>
          <span>{t("securityJournal.period")}</span>
          <select className="select" value={filters.since_minutes} onChange={(event) => patchFilter("since_minutes", event.target.value)}>
            <option value="60">{t("securityJournal.period60")}</option>
            <option value="360">{t("securityJournal.period360")}</option>
            <option value="1440">{t("securityJournal.period1440")}</option>
            <option value="">{t("securityJournal.periodAll")}</option>
          </select>
        </label>
        <label>
          <span>{t("securityJournal.actor")}</span>
          <input className="input" value={filters.actor} onChange={(event) => patchFilter("actor", event.target.value)} />
        </label>
        <label>
          <span>{t("securityJournal.target")}</span>
          <input className="input" value={filters.target} onChange={(event) => patchFilter("target", event.target.value)} />
        </label>
        <label>
          <span>{t("securityJournal.search")}</span>
          <input className="input" value={filters.q} onChange={(event) => patchFilter("q", event.target.value)} />
        </label>
      </div>

      {busy && !items.length ? (
        <div className="settingsJournalEmpty">{t("securityJournal.loading")}</div>
      ) : error ? (
        <div className="settingsJournalEmpty error">{error}</div>
      ) : items.length ? (
        <>
          <div className="settingsAuditList">
            {items.map((event) => (
              <article className={`settingsAuditItem severity-${event.severity || "info"} category-${event.category || "system"}`} key={event.id}>
                <div className="settingsAuditMeta">
                  <time>{formatTimestamp(event.created_at_system || event.created_at, locale)}</time>
                  <span>{event.actor_username || t("securityJournal.systemActor")}</span>
                  <span>{localizedLabel("category", event.category, locale)}</span>
                  <span>{localizedLabel("severity", event.severity, locale)}</span>
                  <span>{auditTarget(event, t)}</span>
                </div>
                <div className="settingsAuditMessage">{auditMessage(event, locale)}</div>
                <div className="settingsAuditEventType">{t("securityJournal.eventType")}: {event.event_type}</div>
              </article>
            ))}
          </div>
          {hasMore ? (
            <button type="button" className="button secondary small settingsAuditLoadMore" onClick={() => loadEvents(offset)} disabled={busy}>
              {busy ? t("common.loading") : t("securityJournal.loadMore")}
            </button>
          ) : null}
        </>
      ) : (
        <div className="settingsJournalEmpty">{t("securityJournal.empty")}</div>
      )}
    </section>
  );
}

export function DiagnosticsEntry() {
  const { currentUser, loading } = useCurrentUser();
  const { locale, t } = useI18n();
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [bugText, setBugText] = useState("");
  const canRunDiagnostics = Boolean(currentUser?.permissions?.includes("run_diagnostics"));

  async function downloadArchive(mode) {
    if (busy) return;
    setBusy(`archive-${mode}`);
    setMessage("");
    try {
      const { blob, filename } = await apiFetchBlob(`/settings/logs/archive?mode=${encodeURIComponent(mode)}`);
      downloadBlob(blob, filename || `km-vms-logs-${mode}.zip`);
      setMessage(t("diagnosticsEntry.archiveReady"));
    } catch (err) {
      setMessage(String(err?.message || t("diagnosticsEntry.failed")));
    } finally {
      setBusy("");
    }
  }

  async function createBugReport() {
    if (busy || !bugText.trim()) return;
    setBusy("bug-report");
    setMessage("");
    try {
      const { blob, filename } = await apiFetchBlob("/settings/bug-report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: bugText.trim(), include_logs: false }),
      });
      downloadBlob(blob, filename || "km-vms-bug-report.zip");
      setMessage(t("diagnosticsEntry.bugReportReady"));
    } catch (err) {
      setMessage(String(err?.message || t("diagnosticsEntry.failed")));
    } finally {
      setBusy("");
    }
  }

  if (loading) {
    return <div className="settingsJournalEmpty">{t("common.loading")}</div>;
  }
  if (!canRunDiagnostics) {
    return <div className="settingsJournalEmpty error">{forbiddenMessage(locale)}</div>;
  }

  return (
    <section className="settingsSecurityModalSection">
      <div className="settingsSecurityModalSectionHead">
        <h2>{t("diagnosticsEntry.title")}</h2>
        <span>{t("diagnosticsEntry.subtitle")}</span>
      </div>

      <div className="settingsActions">
        <button type="button" className="button secondary small" onClick={() => downloadArchive("normal")} disabled={Boolean(busy)}>
          {busy === "archive-normal" ? t("diagnosticsEntry.running") : t("diagnosticsEntry.normalArchive")}
        </button>
        <button type="button" className="button secondary small" onClick={() => downloadArchive("extended")} disabled={Boolean(busy)}>
          {busy === "archive-extended" ? t("diagnosticsEntry.running") : t("diagnosticsEntry.extendedArchive")}
        </button>
      </div>

      <div className="settingsSecurityModalSectionHead">
        <h3>{t("diagnosticsEntry.bugReport")}</h3>
        <span>{t("diagnosticsEntry.safeContent")}</span>
      </div>
      <textarea
        className="input settingsBugReportTextarea"
        value={bugText}
        onChange={(event) => setBugText(event.target.value.slice(0, 10000))}
        placeholder={t("diagnosticsEntry.bugReportPlaceholder")}
        disabled={Boolean(busy)}
      />
      <button type="button" className="button small settingsSecurityModalButton" onClick={createBugReport} disabled={Boolean(busy) || !bugText.trim()}>
        {busy === "bug-report" ? t("diagnosticsEntry.running") : t("diagnosticsEntry.createBugReport")}
      </button>
      {message ? <div className="settingsJournalEmpty">{message}</div> : null}
    </section>
  );
}
