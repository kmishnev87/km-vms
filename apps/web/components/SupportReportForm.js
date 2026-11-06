"use client";

import { useEffect, useId, useRef, useState } from "react";
import { apiFetch, apiFetchBlob } from "../lib/api";
import { useI18n } from "../lib/i18n";
import { supportAttemptKey, supportErrorKey, supportSubmissionId } from "../lib/supportReports";

export default function SupportReportForm({ onToast, onBusyChange, disabled = false }) {
  const { t } = useI18n();
  const [text, setText] = useState("");
  const [mode, setMode] = useState("normal");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState("");
  const [available, setAvailable] = useState(null);
  const [receipt, setReceipt] = useState(null);
  const attempt = useRef(null);
  const busyRef = useRef(false);
  const formId = useId();
  const actionKey = supportAttemptKey(text, mode);
  const accepted = receipt?.key === actionKey;

  useEffect(() => {
    const controller = new AbortController();
    apiFetch("/settings/support/status", { signal: controller.signal })
      .then(value => setAvailable(Boolean(value?.available)))
      .catch(error => { if (error?.name !== "AbortError") setAvailable(false); });
    return () => controller.abort();
  }, []);

  async function act(kind) {
    if (disabled || busyRef.current || !text.trim() || (kind === "send" && (!consent || !available || accepted))) return;
    busyRef.current = true;
    setBusy(kind);
    onBusyChange?.(true);
    try {
      if (kind === "download") {
        const { blob, filename } = await apiFetchBlob("/settings/bug-report", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: text.trim(), mode, include_logs: true }),
        });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = filename || "km-vms-bug-report.zip";
        document.body.appendChild(link);
        link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        onToast?.({ id: `support-download-${Date.now()}`, tone: "success", title: t("supportReport.downloaded") });
      } else {
        if (attempt.current?.key !== actionKey) attempt.current = { key: actionKey, id: supportSubmissionId() };
        const result = await apiFetch("/settings/support/reports", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: text.trim(), mode, consent: true, submission_id: attempt.current.id }),
        });
        if (result?.accepted !== true || result?.report_id !== attempt.current.id) throw new Error("Invalid support receipt");
        setReceipt({ key: actionKey, id: result.report_id });
        onToast?.({ id: `support-sent-${Date.now()}`, tone: "success", title: t("supportReport.sent") });
      }
    } catch (error) {
      onToast?.({
        id: `support-error-${Date.now()}`, tone: "error", title: t("supportReport.failed"),
        message: kind === "send" ? t(`supportReport.${supportErrorKey(error)}`) : t("supportReport.archiveFailed"),
      });
    } finally {
      busyRef.current = false;
      setBusy("");
      onBusyChange?.(false);
    }
  }

  return (
    <div className="supportReportForm" aria-busy={Boolean(busy)}>
      <label className="supportReportLabel" htmlFor={`${formId}-text`}>{t("supportReport.description")}</label>
      <textarea
        id={`${formId}-text`} className="input settingsBugReportTextarea" maxLength={10000}
        value={text} onChange={event => setText(event.target.value)} disabled={Boolean(busy) || disabled}
        placeholder={t("diagnosticsEntry.bugReportPlaceholder")}
      />
      <fieldset className="supportReportModes" disabled={Boolean(busy) || disabled}>
        <legend>{t("supportReport.diagnostics")}</legend>
        {["normal", "extended"].map(value => (
          <label key={value} className={mode === value ? "is-selected" : ""}>
            <input type="radio" name={`${formId}-mode`} value={value} checked={mode === value} onChange={() => setMode(value)} />
            <span>{t(value === "normal" ? "diagnosticsEntry.normalArchive" : "diagnosticsEntry.extendedArchive")}</span>
            <small>{t(value === "normal" ? "supportReport.normalHint" : "supportReport.extendedHint")}</small>
          </label>
        ))}
      </fieldset>
      <p className="supportReportPrivacy">{t("supportReport.privacy")}</p>
      <label className="supportReportConsent"><input type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)} disabled={Boolean(busy) || disabled} /><span>{t("supportReport.consent")}</span></label>
      <div className="supportReportActions">
        <button type="button" className="button small" onClick={() => act("send")} disabled={Boolean(busy) || disabled || !text.trim() || !consent || !available || accepted}>
          {busy === "send" ? t("supportReport.sending") : t("supportReport.send")}
        </button>
        <button type="button" className="button secondary small" onClick={() => act("download")} disabled={Boolean(busy) || disabled || !text.trim()}>
          {busy === "download" ? t("supportReport.creating") : t("supportReport.download")}
        </button>
      </div>
      <div className="supportReportReceipt" aria-live="polite">
        {accepted ? <><span>{t("supportReport.sent")}</span><span className="supportReportNumber">{t("supportReport.number")}: {receipt.id}</span></>
          : <span>{available === false ? t("supportReport.notConfigured") : t("supportReport.explicitOnly")}</span>}
      </div>
    </div>
  );
}
