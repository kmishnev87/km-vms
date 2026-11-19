"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Layout from "../../components/Layout";
import { OperationDialog, OperationToast } from "../../components/OperationFeedback";
import AiRuleEditor from "../../components/AiRuleEditor";
import { RecordingPolicyRow } from "../../components/RecordingPolicyRow";
import { apiFetch, captureAuthSession } from "../../lib/api";
import { useCurrentUser } from "../../lib/currentUser";
import { useI18n } from "../../lib/i18n";
import { useAuthSession } from "../../lib/useAuthSession";
import { useCameraAnchor } from "../../lib/useCameraAnchor";
import {
  draftForCamera, dropRecordingDraft, hasPolicyChanges,
  inlineSaveProblem, recordingPolicyUpdate, saveErrorKey, stageRecordingDraft,
} from "../../lib/recordingCenter";

const base = "/ai-recording";
const json = (value) => ({ method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) });

export default function RecordingCenterPage() {
  const { currentUser } = useCurrentUser();
  const session = useAuthSession();
  const { t, locale } = useI18n();
  const r = (key, params) => t("aiRecording." + key, params);
  const canManage = Boolean(currentUser?.permissions?.includes("manage_cameras"));
  const canSettings = Boolean(currentUser?.permissions?.includes("manage_settings"));
  const [rows, setRows] = useState(null);
  const [emergency, setEmergency] = useState(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState("");
  const [dialog, setDialog] = useState(null);
  const [drafts, setDrafts] = useState({});
  const [rowProblems, setRowProblems] = useState({});
  const [editor, setEditor] = useState(null);
  const [editorProblem, setEditorProblem] = useState("");
  const [toast, setToast] = useState(null);
  const task = useRef("");
  const request = useRef(null);
  useCameraAnchor(Boolean(canManage && rows?.length && !editor));

  const refresh = useCallback(async () => {
    if (!canManage || !session || task.current) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const active = captureAuthSession();
    const current = () => !controller.signal.aborted && active();
    const read = (path) => apiFetch(base + path, { signal: controller.signal });
    const settings = canSettings ? read("/emergency").then(value => {
      if (current()) setEmergency(value);
    }).catch(() => { if (current()) setEmergency(null); }) : Promise.resolve();
    try {
      const value = await read("/cameras");
      if (current()) { setRows(value.cameras); setFailed(false); }
    } catch (_) { if (current()) setFailed(true); }
    await settings;
  }, [canManage, canSettings, session]);

  useEffect(() => {
    setRows(null); setEmergency(null); setFailed(false); setDialog(null);
    setDrafts({}); setRowProblems({}); setEditor(null); setEditorProblem("");
    const poll = () => { if (document.visibilityState === "visible") void refresh(); };
    poll();
    const timer = setInterval(poll, 10000);
    document.addEventListener("visibilitychange", poll);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", poll);
      request.current?.abort();
    };
  }, [refresh]);

  async function mutate(key, action, onSuccess, onError) {
    if (task.current) return;
    task.current = key;
    setBusy(key);
    request.current?.abort();
    const active = captureAuthSession();
    try {
      const result = await action();
      if (active()) onSuccess(result);
    } catch (error) {
      if (active()) onError(error);
    } finally {
      task.current = "";
      setBusy("");
      if (active()) void refresh();
    }
  }

  function notify(key, tone = "success") {
    setToast({ id: "recording-" + Date.now(), title: r(key), tone });
  }

  function changeRow(row, draft) {
    setDrafts(old => stageRecordingDraft(old, row, draft));
    setRowProblems(old => dropRecordingDraft(old, row.camera_id));
  }

  function resetRow(id) {
    setDrafts(old => dropRecordingDraft(old, id));
    setRowProblems(old => dropRecordingDraft(old, id));
  }

  function openEditor(row) {
    const { revision, draft } = drafts[row.camera_id] || { revision:row.policy.revision, draft:draftForCamera(row) };
    setEditorProblem("");
    setEditor({ row, revision, draft });
  }

  function leaveEditor() {
    if (busy || !editor) return;
    const { row, revision, draft } = editor;
    if (hasPolicyChanges(row, draft)) {
      setDrafts(old => ({...old,[row.camera_id]:{revision,draft}}));
    } else if (drafts[row.camera_id]) {
      resetRow(row.camera_id);
    }
    setEditor(null);
  }

  function save(row, entry, inEditor = false) {
    if (!row || !entry || inlineSaveProblem(rows || [], row, entry)) return;
    const requestRow = {...row,policy:{...row.policy,revision:entry.revision}};
    mutate("save:" + row.camera_id, () => apiFetch(base + "/cameras/" + row.camera_id,
      json(recordingPolicyUpdate(requestRow, entry.draft))),
      (updated) => {
        setRows(old => old?.map(item => item.camera_id === updated.camera_id ? updated : item));
        resetRow(row.camera_id);
        if (inEditor) setEditor(null);
        notify("saved");
      }, error => {
        const code = saveErrorKey(error);
        if (inEditor) setEditorProblem(code === "error_changed" ? "row_changed" : code);
        else setRowProblems(old => ({...old,[row.camera_id]:code === "error_changed" ? "row_changed" : code}));
      });
  }

  function toggleEmergency() {
    if (!emergency || !canSettings) return;
    mutate("emergency", () => apiFetch(base + "/emergency", json({ enabled: !emergency.enabled, expected_enabled: emergency.enabled })),
      setEmergency, error => notify(saveErrorKey(error), "error"));
  }

  const close = () => setDialog(null);
  let modal = null;
  if (dialog?.type === "help") {
    modal = {
      id: "recording-help", title: r("help"), tone: "neutral", className: "recordingCenterDialog",
      closeLabel: t("common.close"), content: <div className="recordingCenterFacts">
        <p>{r("help_body")}</p><p>{r("help_buffer")}</p><p>{r("help_link")}</p>
      </div>,
    };
  }

  const headerKeys = ["camera","scenario","row_source","profile","row_before","row_after","row_conditions","status"];
  const liveEditorRow = editor && rows?.find(row => row.camera_id === editor.row.camera_id);
  return <Layout>{canManage ? editor ? <div className="standardPage"><AiRuleEditor camera={editor.row} draft={editor.draft} locale={locale}
    update={draft => { setEditor(old => old ? {...old,draft} : old); setEditorProblem(""); }}
    issue={editorProblem || (liveEditorRow ? inlineSaveProblem(rows, liveEditorRow, editor) : "row_camera_gone")}
    dirty={hasPolicyChanges(editor.row, editor.draft)} saving={Boolean(busy)}
    onBack={leaveEditor} onSave={() => save(liveEditorRow,editor,true)} r={r} t={t} /></div> :
    <div className="standardPage recordingCenter">
      <header className="recordingCenterHead">
        <h1><img src="/assets/icons/dashboard/ai-recording-v2.png" alt="" />{r("title")}</h1>
        <button className="button secondary small" type="button" onClick={() => setDialog({ type: "help" })}>{r("help")}</button>
      </header>
      {canSettings ? <label className="recordingCenterProtection">
        <input type="checkbox" checked={emergency?.enabled === true} disabled={!emergency || Boolean(busy)}
          onChange={toggleEmergency} />
        <span><strong>{r("emergency")}</strong><small>{r(emergency ? "emergency_help" : "emergency_unknown")}</small></span>
      </label> : null}
      <section aria-label={r("camera")} className="recordingCenterList">
        <div className="recordingCenterColumnNames" aria-hidden="true">
          {headerKeys.map(key => <span key={key}>{r(key)}</span>)}<span />
        </div>
        {failed ? <div className="recordingCenterEmpty" role="status">{r("error_load")} <button type="button"
          className="button secondary small" onClick={refresh}>{t("common.refresh")}</button></div> : null}
        {!rows && !failed ? <p className="recordingCenterEmpty" role="status">{r("loading")}</p> : null}
        {rows?.length === 0 && !failed ? <p className="recordingCenterEmpty">{r("empty")}</p> : null}
        {rows?.map(row => <RecordingPolicyRow key={row.camera_id} row={row} rows={rows}
          entry={drafts[row.camera_id]} problem={rowProblems[row.camera_id]}
          busy={Boolean(busy)} saving={busy === "save:"+row.camera_id}
          change={draft => changeRow(row,draft)} reset={() => resetRow(row.camera_id)}
          save={() => save(row,drafts[row.camera_id])} openRules={() => openEditor(row)} r={r} t={t} />)}
      </section>
      <OperationDialog dialog={modal} onClose={close} />
      <OperationToast toast={toast} onClose={() => setToast(null)} />
    </div> : null}</Layout>;
}
