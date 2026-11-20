"use client";

import { useRef, useState } from "react";
import { apiFetch } from "../../lib/api";
import { useI18n } from "../../lib/i18n";
import { supportSubmissionId } from "../../lib/supportReports";
import { OperationDialog } from "../OperationFeedback";
import { useArchiveOperationNotice } from "./ArchiveOperationNotices";

const copyByLocale = {
  ru: { target: "Куда записывать", unavailable: "Недоступно", noTargets: "Нет доступных папок", title: "Перенести старые записи?", keep: "Оставить на месте", move: "Перенести выбранные", help: "Новые места уже сохранены. Отметьте камеры, архив которых нужно перенести в фоне.", failed: "Не удалось поставить перенос в очередь. Записи остались на месте.", someFailed: "Не все папки доступны для переноса. Проверьте состояние хранилищ.", queued: "Перенос поставлен в очередь. Ход работы виден в «Хранилище».", count: "Записей", cameras: "Куда пишут камеры", moves: "Переносы записей камер", waiting: "Ожидает завершения файлов", preparing: "Подготовка", running: "Переносится", done: "Завершён", partial: "Перенесено частично", blocked: "Не выполнен", default: "Папка не назначена" },
  en: { target: "Record to", unavailable: "Unavailable", noTargets: "No available folders", title: "Move older recordings?", keep: "Leave in place", move: "Move selected", help: "New destinations are saved. Choose the cameras whose archives should move in the background.", failed: "Could not queue the move. Recordings remain in place.", someFailed: "Some folders could not be moved. Check Storage for their status.", queued: "Move queued. Track its progress in Storage.", count: "Recordings", cameras: "Recording cameras", moves: "Camera archive moves", waiting: "Waiting for files to close", preparing: "Preparing", running: "Moving", done: "Complete", partial: "Partially moved", blocked: "Not completed", default: "No folder assigned" },
  "zh-CN": { target: "录像保存位置", unavailable: "不可用", noTargets: "无可用文件夹", title: "移动旧录像？", keep: "保留原位置", move: "移动所选", help: "新的保存位置已保存。选择要在后台迁移录像的摄像机。", failed: "无法将迁移加入队列，录像仍在原处。", someFailed: "部分文件夹无法迁移，请检查存储状态。", queued: "迁移已排队，可在存储页面查看进度。", count: "录像", cameras: "录像摄像机", moves: "摄像机录像迁移", waiting: "等待文件关闭", preparing: "正在准备", running: "正在迁移", done: "已完成", partial: "部分迁移", blocked: "未完成", default: "未指定文件夹" },
};

export function useCameraArchiveCopy() {
  const { locale } = useI18n();
  return { ...(copyByLocale[locale] || copyByLocale.en), ...(readinessCopy[locale] || readinessCopy.en), assigned: {
    ru: "Сначала выберите другую папку для камер, назначенных в эту папку",
    en: "Assign these cameras to another folder before removing this one",
    "zh-CN": "请先将摄像机分配到其他文件夹",
  }[locale] || "Assign these cameras to another folder before removing this one" };
}

const readinessCopy = {
  ru: {
    ready: "Готова к записи", provisioning: "Подключается", removing: "Отключается", read_only: "Только чтение",
    low_space: "Мало места", stateTitle: "Статус папки", choose: "Выбрать папку",
    mountMissing: "Папка ещё не подключена к KM VMS. Проверьте том или добавьте эту папку повторно.",
    attachStarted: "Папка появится в настройках камер после подключения.",
    overflow: "Предупреждение < 10%, очистка < 5% до 9%, пауза записи < 1%.",
    failover: "Аварийная запись", failoverHelp: "При сбое запись продолжится на томе, где больше всего свободного места.",
    readyElsewhere: "Запись доступна на исправных томах.",
  },
  en: {
    ready: "Ready to record", provisioning: "Connecting", removing: "Removing", read_only: "Read-only",
    low_space: "Low on space", stateTitle: "Folder status", choose: "Choose a folder",
    mountMissing: "This folder is not connected to KM VMS. Check the volume or add the folder again.",
    attachStarted: "The folder will be available to cameras once connected.",
    overflow: "Warn below 10%, clean below 5% to 9%, pause recording below 1%.",
    failover: "Emergency recording", failoverHelp: "On failure, record to the available volume with the most free space.",
    readyElsewhere: "Recording is available on healthy volumes.",
  },
  "zh-CN": {
    ready: "可录像", provisioning: "正在连接", removing: "正在移除", read_only: "只读", low_space: "空间不足",
    stateTitle: "文件夹状态", choose: "选择文件夹",
    mountMissing: "文件夹尚未连接到 KM VMS。请检查存储卷或重新添加文件夹。",
    attachStarted: "连接后即可为摄像机选择此文件夹。",
    overflow: "低于 10% 警告，低于 5% 清理至 9%，低于 1% 暂停录像。",
    failover: "应急录像", failoverHelp: "故障时，使用剩余空间最多的可用卷继续录像。",
    readyElsewhere: "正常的存储卷仍可录像。",
  },
};

export function ArchiveRootSelect({ catalog, value, onChange, disabled, label, requireSelection = false }) {
  const copy = useCameraArchiveCopy();
  const selected = value || (!requireSelection ? catalog?.default_root_id : "") || "";
  const items = catalog?.items || [];
  const current = items.find(item => item.id === selected);
  return <select className="input cameraArchiveTargetSelect" aria-label={label || copy.target} title={current?.path}
    value={selected} disabled={disabled || !items.some(item => item.writable)}
    onChange={event => onChange(event.target.value)}>
    {!current ? <option value={selected} disabled>{selected ? copy.unavailable : requireSelection ? copy.choose : copy.default}</option> : null}
    {items.map(item => <option key={item.id} value={item.id} disabled={!item.writable}>
      {item.path}{!item.writable ? " — " + (copy[item.state] || copy.unavailable) : ""}
    </option>)}
  </select>;
}

export function ArchiveMoveDialog({ offers, onClose, onQueued }) {
  const copy = useCameraArchiveCopy();
  const notice = useArchiveOperationNotice();
  const [selected, setSelected] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requestId = useRef(null);
  if (!offers.length) return null;
  async function confirm() {
    if (busy || !selected.length) return;
    setBusy(true); setError("");
    try {
      requestId.current ||= supportSubmissionId();
      const value = await apiFetch("/cameras/archive-moves", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId.current, cameras: offers.filter(item => selected.includes(item.camera_id))
          .map(({camera_id, target_root_id}) => ({ camera_id, target_root_id })) }),
      });
      const planIds = (value?.plans || []).map(plan => plan.plan_id).filter(Boolean);
      if (!planIds.length) {
        setError(value?.skipped?.length ? copy.someFailed : copy.failed);
        return;
      }
      notice({ id: requestId.current, planIds, skipped: Boolean(value.skipped?.length) });
      onQueued?.(value);
      onClose();
    } catch {
      setError(copy.failed);
    } finally { setBusy(false); }
  }
  const content = <div className="cameraArchiveMoveContent">
    <p>{copy.help}</p>
    <div className="cameraArchiveMoveRows">
      {offers.map(item => <label key={item.camera_id} className="cameraArchiveMoveRow">
        <input type="checkbox" checked={selected.includes(item.camera_id)} disabled={busy}
          onChange={event => {
            requestId.current = null; setError("");
            setSelected(values => event.target.checked ? [...values, item.camera_id] : values.filter(id => id !== item.camera_id));
          }} />
        <span><strong>{item.camera_name}</strong><span className="cameraArchiveMovePath">{item.target_path}</span></span>
        <small>{copy.count}: {item.recording_count}</small>
      </label>)}
    </div>
    {error ? <p role="alert" className="archiveRulesError">{error}</p> : null}
  </div>;
  return <OperationDialog dialog={{
    id: "camera-archive-move", title: copy.title, tone: "neutral", className: "cameraArchiveMoveDialog",
    overlayClassName: "cameraEventsDetailOverlay", content, busy,
    closeLabel: copy.keep,
    cancelLabel: copy.keep, confirmLabel: copy.move, confirmDisabled: !selected.length,
    onConfirm: confirm,
  }} onClose={onClose}/>;
}
