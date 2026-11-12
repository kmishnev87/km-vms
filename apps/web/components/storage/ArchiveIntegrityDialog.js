"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import { useModalBodyScrollLock } from "../OperationFeedback";
import { useI18n } from "../../lib/i18n";
import { archiveIntegrityFindingPresentation, archiveIntegrityScanModel, formatBytes } from "../../lib/storageOperations";
import { formatProductDateTime } from "../../lib/timezone";
import { isReviewRunning, reviewFailureText } from "../../lib/useArchiveIntegrityReviewController";

function focusable(container) {
  return Array.from(container?.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), [href], [tabindex="0"]') || [])
    .filter((node) => !node.hidden && node.getAttribute("aria-hidden") !== "true" && node.getClientRects().length);
}

function nextDay(value) {
  if (!value) return "";
  const date = new Date(`${value}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString().slice(0, 10);
}

export default function ArchiveIntegrityDialog({ controller: c, copy, language, permission }) {
  const { t } = useI18n();
  const dialog = useRef(null);
  const returnFocus = useRef(null);
  const [expanded, setExpanded] = useState([]);
  const [action, setAction] = useState("recheck");
  const { integrityDialogOpen: open, integrityScan: scan, page, filters, selected, allMatching, batch, confirmation } = c;
  useModalBodyScrollLock(open);
  const model = archiveIntegrityScanModel(scan || {}, permission);
  const working = isReviewRunning(batch);
  const locked = c.integrityBusy || c.loading || c.listFailed || working || model.running || !permission.allowed;
  const selectable = page.items.filter((item) => item.actions?.length);
  const pageSelected = selectable.length > 0 && selectable.every((item) => c.isSelected(item.finding_id));

  useEffect(() => {
    if (!open) return undefined;
    returnFocus.current = document.activeElement;
    const timer = window.setTimeout(() => (focusable(dialog.current)[0] || dialog.current)?.focus({ preventScroll: true }), 0);
    return () => { window.clearTimeout(timer); returnFocus.current?.isConnected && returnFocus.current.focus({ preventScroll: true }); };
  }, [open]);
  useEffect(() => { setExpanded([]); }, [scan?.scan_id, JSON.stringify(filters)]);

  if (!open) return null;
  const filter = (key, value) => c.setFilters((previous) => ({ ...previous, [key]: value }));
  const checkedAt = page.last_checked_at;
  const dateTime = (value) => formatProductDateTime(value, page.display_timezone, language === "en" ? "en-US" : language === "zh-CN" ? "zh-CN" : "ru-RU") || "—";
  const categoryLabel = (category) => copy[archiveIntegrityFindingPresentation({ category }).categoryKey] || copy.integrityCategoryUnknown;
  const resultMessage = (item) => {
    if (item.reason_code) return reviewFailureText(item.reason_code, t);
    if (item.result?.resolved || item.result?.already_resolved) return t("archiveReview.resultText.resolved");
    if (batch.action === "recheck") return t("archiveReview.resultText.confirmed");
    if (item.result?.retired_count) return t("archiveReview.resultText.retired");
    return t(`archiveReview.resultText.${batch.action}`);
  };
  function onKeyDown(event) {
    if (event.key === "Escape" && !c.integrityBusy) {
      event.preventDefault(); event.stopPropagation();
      if (confirmation) c.setConfirmation(null);
      else if (c.resultOpen) c.setResultOpen(false);
      else c.setIntegrityDialogOpen(false);
    }
    if (event.key === "Tab") {
      const nodes = focusable(dialog.current);
      const first = nodes[0], last = nodes.at(-1);
      if (!nodes.length) { event.preventDefault(); dialog.current?.focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  }

  return (
    <div className="storageIntegrityOverlay" role="presentation">
      <section ref={dialog} className="storageIntegrityDialog archiveReviewDialog" role="dialog" aria-modal="true" aria-labelledby="archive-review-title" tabIndex={-1} onKeyDown={onKeyDown}>
        <header className="storageIntegrityDialogHead">
          <h2 id="archive-review-title">{copy.integrityModalTitle}</h2>
          <button className="storageIntegrityClose" type="button" onClick={() => c.setIntegrityDialogOpen(false)} disabled={c.integrityBusy} aria-label={copy.close}>×</button>
        </header>
        <div className="archiveReviewSummary">
          <div><strong>{copy[model.titleKey] || copy.integrityScanNotRunTitle}</strong><span>{t("archiveReview.summaryHint")}</span></div>
          {checkedAt ? <time dateTime={checkedAt}>{t("archiveReview.lastFullCheck")}: {dateTime(checkedAt)}</time> : null}
          {model.running ? <progress max="100" value={model.progressIndeterminate ? undefined : model.percent} aria-label={copy.integrityProgress} /> : null}
        </div>
        <div className="archiveReviewFilters" aria-label={t("archiveReview.filters")}>
          <label>{copy.camera}<select value={filters.camera_id || ""} onChange={(e) => filter("camera_id", e.target.value)} disabled={c.integrityBusy || working}>
            <option value="">{t("archiveReview.allCameras")}</option>{page.cameras.map((camera) => <option value={camera.id} key={camera.id}>{camera.name}</option>)}
          </select></label>
          <label>{t("archiveReview.problemType")}<select value={filters.category || ""} onChange={(e) => filter("category", e.target.value)} disabled={c.integrityBusy || working}>
            <option value="">{t("archiveReview.allTypes")}</option>{page.categories.map((category) => <option key={category} value={category}>{categoryLabel(category)}</option>)}
          </select></label>
          <label>{t("archiveReview.reviewState")}<select value={filters.state} onChange={(e) => filter("state", e.target.value)} disabled={c.integrityBusy || working}>
            {["active", "acknowledged", "resolved", "all"].map((state) => <option key={state} value={state}>{t(`archiveReview.states.${state}`)}{state !== "all" ? ` (${page.review_counts[state] || 0})` : ""}</option>)}
          </select></label>
          <label className="archiveReviewSearch">{t("archiveReview.search")}<input value={filters.search || ""} onChange={(e) => filter("search", e.target.value)} placeholder={t("archiveReview.searchPlaceholder")} disabled={c.integrityBusy || working} /></label>
          <label>{t("archiveReview.dateFrom")}<input type="date" value={filters.date_from || ""} onChange={(e) => filter("date_from", e.target.value)} disabled={c.integrityBusy || working} /></label>
          <label>{t("archiveReview.dateTo")}<input type="date" value={filters.date_to ? (() => { const date = new Date(`${filters.date_to}T12:00:00Z`); date.setUTCDate(date.getUTCDate() - 1); return date.toISOString().slice(0, 10); })() : ""} onChange={(e) => filter("date_to", nextDay(e.target.value))} disabled={c.integrityBusy || working} /></label>
          {page.roots.length > 1 ? <label>{copy.integrityRootLabel}<select value={filters.root_id || ""} onChange={(e) => filter("root_id", e.target.value)} disabled={c.integrityBusy || working}><option value="">{t("archiveReview.allRoots")}</option>{page.roots.map((root) => <option key={root.id} value={root.id}>{root.name}</option>)}</select></label> : null}
        </div>
        <div className="archiveReviewSelection">
          <span>{t("archiveReview.shown", { count: page.items.length, total: page.total_count })}</span>
          <span>{t("archiveReview.selected", { count: c.selectedCount })}</span>
          {page.total_count > 0 ? <button type="button" className="archiveReviewTextButton" onClick={() => c.setAllMatching(!allMatching)} disabled={locked}>{allMatching ? t("archiveReview.clearSelection") : t("archiveReview.selectMatching", { count: page.total_count })}</button> : null}
          {batch && !working ? <button type="button" className="archiveReviewTextButton" onClick={() => c.setResultOpen(!c.resultOpen)}>{c.resultOpen ? t("archiveReview.backToList") : t("archiveReview.actionResults")}</button> : null}
          {!batch && c.lastBatch ? <button type="button" className="archiveReviewTextButton" onClick={c.showLastResults}>{t("archiveReview.actionResults")}</button> : null}
          <button type="button" className="archiveReviewTextButton archiveReviewRefresh" onClick={() => c.refreshReview().catch(() => {})} disabled={c.integrityBusy || c.loading}>{copy.refresh}</button>
        </div>
        <div className="archiveReviewViewport" aria-busy={c.loading ? "true" : "false"}>
          {c.resultOpen && batch ? (
            <div className="archiveReviewResults">
              <h3>{t("archiveReview.actionResults")}</h3>
              {(batch.items || []).map((item) => <article key={item.id} className={`archiveReviewResult ${item.state}`}>
                <div><strong>{item.camera_name || item.display_name}</strong><span>{item.display_name}</span></div>
                <div><strong>{t(`archiveReview.resultStates.${item.state}`)}</strong><span>{resultMessage(item)}</span></div>
              </article>)}
              {batch.next_cursor ? <button type="button" className="button secondary small" onClick={c.loadMoreResults} disabled={c.integrityBusy}>{copy.integrityLoadMore}</button> : null}
            </div>
          ) : page.items.length ? (
            <table className="archiveReviewTable">
              <colgroup><col className="reviewSelectCol" /><col className="reviewCameraCol" /><col className="reviewProblemCol" /><col className="reviewDateCol" /><col className="reviewStateCol" /><col className="reviewActionCol" /></colgroup>
              <thead><tr>
                <th><input type="checkbox" aria-label={t("archiveReview.selectPage")} checked={pageSelected} ref={(node) => { if (node) node.indeterminate = !pageSelected && selectable.some((item) => c.isSelected(item.finding_id)); }} onChange={c.togglePage} disabled={locked || !selectable.length} /></th>
                <th>{copy.camera}</th><th>{t("archiveReview.problemType")}</th><th>{t("archiveReview.recordedAt")}</th><th>{t("archiveReview.reviewState")}</th><th>{t("archiveReview.action")}</th>
              </tr></thead>
              <tbody>{page.items.map((finding) => {
                const row = archiveIntegrityFindingPresentation(finding);
                const expandedRow = expanded.includes(finding.finding_id);
                return <Fragment key={finding.finding_id}>
                  <tr className={finding.state === "active" ? "" : "archiveReviewReviewed"}>
                    <td><input type="checkbox" aria-label={t("archiveReview.selectRecording", { name: row.displayName || row.cameraName })} checked={c.isSelected(finding.finding_id)} onChange={() => c.toggleSelected(finding.finding_id)} disabled={locked || !finding.actions?.length} /></td>
                    <td><strong>{row.cameraName || "—"}</strong><span className="archiveReviewFilename" title={row.displayName}>{row.displayName}</span></td>
                    <td><button type="button" className="archiveReviewProblemButton" aria-expanded={expandedRow} onClick={() => setExpanded((previous) => expandedRow ? previous.filter((id) => id !== finding.finding_id) : [...previous, finding.finding_id])}>{copy[row.categoryKey] || copy.integrityCategoryUnknown}<span aria-hidden="true">{expandedRow ? "−" : "+"}</span></button></td>
                    <td>{dateTime(finding.recording_started_at)}</td>
                    <td><span className={`archiveReviewState ${finding.state}`}>{t(`archiveReview.states.${finding.state}`)}</span></td>
                    <td>{finding.actions?.length ? <select className="archiveReviewRowAction" value="" aria-label={t("archiveReview.actionsFor", { name: row.cameraName || row.displayName })} onChange={(e) => { if (e.target.value) c.prepareAction(e.target.value, finding); }} disabled={locked}>
                      <option value="">{t("archiveReview.chooseAction")}</option>{finding.actions.map((key) => <option key={key} value={key}>{t(`archiveReview.actions.${key}`)}</option>)}
                    </select> : <span className="archiveReviewNoAction">{finding.state === "resolved" ? t("archiveReview.noFurtherAction") : t("archiveReview.requiresAttention")}</span>}</td>
                  </tr>
                  {expandedRow ? <tr className="archiveReviewDetailsRow"><td colSpan="6"><div className="archiveReviewDetails">
                    <p className="archiveReviewDetailExplanation">{copy[row.detailKey] || copy[row.impactKey] || copy.integrityImpactUnknown}</p>
                    <dl>
                      <div className="archiveReviewDetailWide"><dt>{copy.integrityFileLabel}</dt><dd>{row.displayName || "—"}</dd></div>
                      <div className="archiveReviewDetailWide"><dt>{copy.integrityRootLabel}</dt><dd>{row.rootLabel || "—"}</dd></div>
                      <div><dt>{t("archiveReview.recordedAt")}</dt><dd>{dateTime(finding.recording_started_at)}</dd></div>
                      <div><dt>{t("archiveReview.fileSize")}</dt><dd>{formatBytes(finding.size_bytes)}</dd></div>
                      <div><dt>{t("archiveReview.observedAt")}</dt><dd>{dateTime(finding.observed_at)}</dd></div>
                      <div><dt>{t("archiveReview.reviewState")}</dt><dd>{t(`archiveReview.states.${finding.state}`)}</dd></div>
                    </dl>
                    {finding.reason_code && finding.reason_code !== "probe_ok" ? <div className="archiveReviewDetailResult"><strong>{t("archiveReview.checkResult")}</strong><p>{reviewFailureText(finding.reason_code, t)}</p></div> : null}
                    {!finding.actions?.length && finding.state === "active" ? <p className="archiveReviewDetailAvailability">{copy[row.noActionLabelKey] || copy.integrityNoActionUnavailable}</p> : null}
                  </div></td></tr> : null}
                </Fragment>;
              })}</tbody>
            </table>
          ) : <div className="archiveReviewEmpty">{c.loading || c.integrityBusy ? copy.loading : c.listFailed ? t("archiveReview.listFailed") : model.status === "not_run" ? copy.integrityScanNotRunTitle : t("archiveReview.emptyFilter")}</div>}
          {!c.resultOpen && page.next_cursor ? <div className="archiveReviewMore"><button type="button" className="button secondary small" onClick={c.loadMoreIntegrityFindings} disabled={locked}>{copy.integrityLoadMore}</button></div> : null}
        </div>
        <footer className="archiveReviewFooter">
          <div className="archiveReviewActionArea">
            {confirmation ? <>
              <div className="archiveReviewConfirmation" role="status"><strong>{t(`archiveReview.actions.${confirmation.action}`)}: {confirmation.eligible_count}</strong><span>{t(`archiveReview.effects.${confirmation.action}`)}{confirmation.action === "delete" ? ` ${formatBytes(confirmation.total_bytes)}.` : ""}{confirmation.unavailable_count ? ` ${t("archiveReview.unavailableCount", { count: confirmation.unavailable_count })}` : ""}</span></div>
              <div className="archiveReviewActionButtons"><button type="button" className="button secondary small" onClick={() => c.setConfirmation(null)} disabled={c.integrityBusy}>{copy.cancel}</button><button type="button" className={`button small ${confirmation.destructive ? "dangerButton" : ""}`} onClick={c.applyAction} disabled={c.integrityBusy || !confirmation.eligible_count}>{c.integrityBusy ? copy.applying : t("archiveReview.confirmAction")}</button></div>
            </> : batch ? <>
              <div className="archiveReviewBatch" role="status"><strong className={working ? "" : batch.state === "completed" ? "reviewComplete" : "reviewAttention"}>{t(`archiveReview.batchStates.${batch.state}`)}</strong><span>{t("archiveReview.processed", { count: batch.processed_count, total: batch.planned_count })}{batch.failed_count ? ` ${t("archiveReview.notCompleted", { count: batch.failed_count })}` : ""}</span><progress max={batch.planned_count || 1} value={batch.processed_count || 0} className={batch.state === "completed" ? "isComplete" : ""} /></div>
              <div className="archiveReviewActionButtons">{working ? <button type="button" className="button secondary small" disabled={c.integrityBusy || batch.state === "cancel_requested"} onClick={c.cancelBatch}>{t("archiveReview.stop")}</button> : <>{batch.failed_count ? <button type="button" className="button secondary small" onClick={c.retryFailed} disabled={c.integrityBusy}>{t("archiveReview.retryFailed")}</button> : null}<button type="button" className="button secondary small" onClick={c.dismissBatch}>{t("archiveReview.done")}</button></>}</div>
            </> : <>
              <div className="archiveReviewBulk"><label>{t("archiveReview.forSelected")}<select value={action} onChange={(e) => setAction(e.target.value)} disabled={locked}>{c.allowedActions.map((key) => <option key={key} value={key}>{t(`archiveReview.actions.${key}`)}</option>)}</select></label><button type="button" className="button small" onClick={() => c.prepareAction(action)} disabled={locked || !c.selectedCount || !c.allowedActions.includes(action)}>{t("archiveReview.runForSelected", { count: c.selectedCount })}</button></div>
              <span className="archiveReviewBulkHint">{t("archiveReview.bulkHint")}</span>
            </>}
          </div>
          <div className="archiveReviewFooterBottom"><button className="button secondary small" type="button" onClick={() => c.setIntegrityDialogOpen(false)} disabled={c.integrityBusy}>{copy.close}</button><div>{model.canCancel ? <button type="button" className="button secondary small" onClick={c.cancelIntegrityScan} disabled={c.integrityBusy}>{copy.integrityCancelScan}</button> : null}{model.canStart ? <button type="button" className="button secondary small" onClick={c.startIntegrityScan} disabled={c.integrityBusy || working || !permission.allowed}>{model.status === "not_run" ? copy.integrityCheckArchive : copy.integrityCheckAgain}</button> : null}</div></div>
        </footer>
      </section>
    </div>
  );
}
