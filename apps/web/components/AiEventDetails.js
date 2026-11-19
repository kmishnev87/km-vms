"use client";
import { useEffect, useState } from "react";
import { apiFetch, apiFetchBlob, captureAuthSession } from "../lib/api";
import { eventLabelKey, eventObjectLabelKeys } from "../lib/cameraEvents";
import { buildArchiveExportPayload } from "../lib/archiveExports";
import { formatProductDateTime, productDateTimeInputValue, productInstantInputValue } from "../lib/timezone";
import { useI18n } from "../lib/i18n";
import useArchiveExport from "../lib/useArchiveExport";
import ArchiveExportDialog from "./ArchiveExportDialog";
import { OperationDialog } from "./OperationFeedback";

export default function AiEventDetails({ event, timezone, canReview, onClose, onChanged, busy, onOpen }) {
  const { t, locale } = useI18n();
  const copy = key => t(`cameraEvents.ai.${key}`);
  const [evidence, setEvidence] = useState(null);
  const [image, setImage] = useState(null);
  const [revision, setRevision] = useState(0);
  const [reviewing, setReviewing] = useState(false);
  const [changed, setChanged] = useState(null);
  const [error, setError] = useState("");
  const [draft, setDraft] = useState(null);
  const controller = useArchiveExport();
  const path = `/camera-events/${encodeURIComponent(event.id)}`;
  const row = changed || evidence?.event || event;
  const state = evidence?.archive?.state;

  useEffect(() => {
    const request = new AbortController(), current = captureAuthSession();
    apiFetch(`${path}/evidence`, { signal: request.signal }).then(value => {
      if (!request.signal.aborted && current()) { setEvidence(value); setError(""); }
    }).catch(() => { if (!request.signal.aborted && current()) setError("checkFailed"); });
    return () => request.abort();
  }, [path, revision]);

  useEffect(() => {
    if (!row.ai?.snapshot_available) return undefined;
    const request = new AbortController(), current = captureAuthSession();
    let url;
    apiFetchBlob(`${path}/snapshot`, { signal: request.signal }).then(({ blob }) => {
      if (!request.signal.aborted && current() && blob.type === "image/jpeg" && blob.size <= 128 * 1024) {
        url = URL.createObjectURL(blob);
        setImage(url);
      }
    }).catch(() => {});
    return () => { request.abort(); if (url) URL.revokeObjectURL(url); };
  }, [path, row.ai?.snapshot_available]);

  async function review() {
    const current = captureAuthSession();
    setReviewing(true); setError("");
    try {
      const value = await apiFetch(`${path}/review`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ false_positive: !row.ai.false_positive }) });
      if (current()) { setChanged(value); onChanged(); }
    } catch (_) { if (current()) setError("reviewHelp"); }
    finally { if (current()) setReviewing(false); }
  }

  function openClip() {
    const clip = evidence?.archive?.clip;
    if (!clip) return;
    setDraft({ cameraId: String(clip.camera_id), title: `${copy("details")} · ${row.camera_name}`, reason: "",
      startTs: clip.start_ts, endTs: clip.end_ts });
  }

  if (draft) return <ArchiveExportDialog draft={draft} onDraftChange={setDraft} source="event"
    cameras={[{ id: row.camera_id, name: row.camera_name }]} controller={controller}
    formatInput={value => /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? productInstantInputValue(value, timezone) : productDateTimeInputValue(value)}
    onClose={() => { controller.stopWaiting(); setDraft(null); }}
    onCreate={() => {
      if (!controller.busy) controller.prepare(buildArchiveExportPayload(draft));
    }}/>;

  return <OperationDialog dialog={{
    id: "ai-event-evidence", title: copy("details"), tone: "neutral", className: "cameraAiEvidenceDialog",
    closeLabel: t("cameraEvents.close"), showFooterClose: false,
    content: <div className="cameraAiEvidence">
      <div className="cameraAiEvidenceFrame">{image && row.ai.snapshot_available ? <img src={image} alt={copy("frame")}/> : <span>{copy("noFrame")}</span>}</div>
      <div className="cameraAiEvidenceSummary"><strong>{row.camera_name}</strong><time dateTime={row.occurred_at}>{formatProductDateTime(row.occurred_at, timezone, locale)}</time></div>
      <dl>
        <div><dt>{t("cameraEvents.type")}</dt><dd>{t(eventLabelKey(row))}{eventObjectLabelKeys(row).map(key => ` · ${t(key)}`).join("")}</dd></div>
        <div><dt>{copy("rule")}</dt><dd>{row.ai.rule_name || copy(row.ai.scope === "zone" ? "unnamedZone" : row.ai.scope === "line" ? "unnamedLine" : "wholeFrame")}</dd></div>
      </dl>
      <p className="cameraAiEvidenceState" role="status">{error
        ? (error === "reviewHelp" ? t("cameraEvents.errors.request_failed") : copy(error))
        : state ? copy(state) : t("cameraEvents.loading")}</p>
      <div className="cameraAiEvidenceButtons">
        <button className="button secondary small" type="button" disabled={busy || !row.camera_id} onClick={() => onOpen("live")}>{t("cameraEvents.watchNow")}</button>
        {state === "ready" ? <button className="button small" type="button" disabled={busy} onClick={() => onOpen("archive")}>{t("cameraEvents.watchEvent")}</button>
          : state !== "forbidden" ? <button className="button secondary small" type="button" onClick={() => setRevision(value => value + 1)}>{copy("check")}</button> : null}
        {evidence?.archive?.clip ? <button className="button secondary small" type="button" title={copy("clipHelp")} onClick={openClip}>{copy("saveClip")}</button> : null}
      </div>
      {(canReview || row.ai.false_positive) ? <div className="cameraAiEvidenceReview">
        <span>{row.ai.false_positive ? copy("falsePositive") : copy("reviewHelp")}</span>
        {canReview ? <button className="button secondary small" type="button" disabled={reviewing} onClick={review}>{copy(row.ai.false_positive ? "unmarkFalse" : "markFalse")}</button> : null}
      </div> : null}
    </div>,
  }} onClose={onClose}/>;
}
