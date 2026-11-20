"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../../lib/api";
import { useI18n } from "../../lib/i18n";
import { humanBlockerReason } from "../../lib/storageOperations";
import { supportSubmissionId } from "../../lib/supportReports";
import { OperationDialog } from "../OperationFeedback";
import { useArchiveOperationNotice } from "./ArchiveOperationNotices";

const strings = {
  ru: {
    title: "Отключить папку архива", loading: "Проверяем папку…", cameras: "Камеры, которые пишут сюда",
    records: "Записей в этой папке", target: "Другая папка архива", choose: "Выберите папку",
    move: "Перенести записи в выбранную папку", noTarget: "Сначала добавьте другую папку, готовую к записи.",
    cancel: "Отмена", save: "Продолжить", remove: "Отключить папку", delete: "Подтвердить удаление",
    warning: count => `Записи в этой папке будут удалены безвозвратно. Количество: ${count}.`,
    changed: "Состав архива изменился. Проверьте новое количество и подтвердите ещё раз.",
    failed: "Не удалось начать отключение папки.", retry: "Проверить снова",
    untouched: "Сам том и сторонние файлы не удаляются.",
  },
  en: {
    title: "Remove archive folder", loading: "Checking the folder…", cameras: "Cameras recording here",
    records: "Recordings in this folder", target: "Another archive folder", choose: "Choose a folder",
    move: "Move recordings to the selected folder", noTarget: "First add another folder that is ready for recording.",
    cancel: "Cancel", save: "Continue", remove: "Remove folder", delete: "Confirm deletion",
    warning: count => `Recordings in this folder will be permanently deleted. Count: ${count}.`,
    changed: "The archive changed. Review the new count and confirm again.",
    failed: "Could not start removing this folder.", retry: "Check again",
    untouched: "The volume and unrelated files are not deleted.",
  },
  "zh-CN": {
    title: "移除归档文件夹", loading: "正在检查文件夹…", cameras: "写入此文件夹的摄像机",
    records: "此文件夹中的录像", target: "其他归档文件夹", choose: "选择文件夹",
    move: "将录像迁移到所选文件夹", noTarget: "请先添加一个可录像的文件夹。",
    cancel: "取消", save: "继续", remove: "移除文件夹", delete: "确认删除",
    warning: count => `此文件夹中的录像将被永久删除，共 ${count} 条。`,
    changed: "归档内容已更改。请检查新的数量，然后重新确认。",
    failed: "无法开始移除文件夹。", retry: "重新检查",
    untouched: "不会删除存储卷及其他文件。",
  },
};

export default function ArchiveRootRemovalDialog({ rootId, onClose, onAccepted }) {
  const { locale } = useI18n();
  const copy = strings[locale] || strings.en;
  const notice = useArchiveOperationNotice();
  const [preview, setPreview] = useState(null);
  const [refresh, setRefresh] = useState(0);
  const [target, setTarget] = useState("");
  const [move, setMove] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requestId = useRef(null);
  const url = `/storage/archive-roots/${encodeURIComponent(rootId)}/removal`;

  useEffect(() => {
    const controller = new AbortController();
    apiFetch(url, { signal: controller.signal }).then(value => {
      if (controller.signal.aborted) return;
      setPreview(value);
      setTarget(previous => value.targets.some(item => item.id === previous) ? previous : "");
    }).catch(() => { if (!controller.signal.aborted) setError(copy.failed); });
    return () => controller.abort();
  }, [url, refresh, copy.failed]);

  const recordingCount = preview?.recording_count || 0;
  const needsTarget = Boolean(preview?.requires_target || move);
  const blocked = !preview || !!preview.operation || (needsTarget && !target);
  const formatCount = count => Number(count).toLocaleString(locale);
  const reset = () => { requestId.current = null; setConfirming(false); setError(""); };
  async function submit() {
    if (busy || blocked) return;
    if (!move && recordingCount && !confirming) {
      setConfirming(true);
      return;
    }
    setBusy(true); setError("");
    requestId.current ||= supportSubmissionId();
    try {
      const result = await apiFetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId.current, scope_hash: preview.scope_hash,
          recording_count: recordingCount, target_root_id: needsTarget ? target : null,
          mode: move ? "move" : "delete", confirm_delete: confirming }),
      });
      notice({ id: result.id, rootId });
      onAccepted?.();
      onClose();
    } catch (failure) {
      if (failure?.code === "archive_root_removal_changed") {
        reset(); setPreview(null); setError(copy.changed); setRefresh(value => value + 1);
      } else setError(failure?.code ? humanBlockerReason(failure.code, locale) : copy.failed);
    } finally { setBusy(false); }
  }

  const body = <div className="archiveRootRemovalContent">
    {preview ? <>
      <div className="archiveRootRemovalSummary">
        <strong>{preview.path}</strong>
        <span>{copy.records}: <b>{formatCount(recordingCount)}</b></span>
        {preview.cameras.length ? <span><b>{copy.cameras}:</b> {preview.cameras.map(camera => camera.name).join(", ")}</span> : null}
      </div>
      {preview.requires_target || recordingCount ? <>
        <label className="archiveRootRemovalDestination">
          <span>{copy.target}</span>
          <select className="input" value={target} disabled={busy || confirming || !preview.targets.length || !needsTarget}
            onChange={event => { reset(); setTarget(event.target.value); }}>
            <option value="">{copy.choose}</option>
            {preview.targets.map(item => <option key={item.id} value={item.id}>{item.path}</option>)}
          </select>
        </label>
        {recordingCount > 0 ? <label className="archiveRootRemovalChoice">
          <input type="checkbox" checked={move} disabled={busy || confirming || !preview.targets.length}
            onChange={event => { reset(); setMove(event.target.checked); }} />
          {copy.move}
        </label> : null}
        {needsTarget && !preview.targets.length ? <p role="status">{copy.noTarget}</p> : null}
      </> : null}
      <p>{copy.untouched}</p>
      {confirming ? <p className="archiveRootRemovalWarning" role="alert">{copy.warning(formatCount(recordingCount))}</p> : null}
    </> : <p role="status">{copy.loading}</p>}
    {error ? <p className="archiveRootRemovalWarning" role="alert">{error}
      {!preview && error === copy.failed ? <button type="button" className="button secondary small"
        onClick={() => { setError(""); setRefresh(n => n + 1); }}>{copy.retry}</button> : null}
    </p> : null}
  </div>;

  return <OperationDialog dialog={{ id: `archive-root-removal-${rootId}`, title: copy.title,
    className: "archiveRootRemovalDialog", tone: confirming ? "warning" : "neutral",
    confirmTone: confirming ? "danger" : undefined, closeLabel: copy.cancel, cancelLabel: copy.cancel,
    confirmLabel: confirming ? copy.delete : recordingCount && !move ? copy.save : copy.remove,
    content: body, busy, confirmDisabled: blocked, onConfirm: submit }} onClose={onClose} />;
}
