const COPY = {
  ru: {
    title: "Применение формата записи",
    idle: "При работающих камерах можно выбрать: дождаться конца файлов или применить формат сейчас.",
    pending: "Выбор сохранён. Ждём перехода камер на {format}.",
    applied: "Запись в формате {format} подтверждена на всех включённых камерах.",
    configured: "Настройка сохранена. Новые записи будут в формате {format}.",
    failed: "Не для всех камер подтверждено применение {format}. Откройте подробности, чтобы узнать причину.",
    details: "Подробнее",
    close: "Закрыть",
    waiting_segment: "ждём окончания текущего файла",
    switching: "завершаем прежний файл и переключаем запись",
    starting: "запускаем запись и ждём видео",
    waiting: "ожидаем возобновления записи",
    appliedCamera: "новый формат подтверждён",
    previous_segment_not_finalized: "не подтверждено завершение прежнего файла; уже сохранённое видео не удалено",
    finish_timeout: "прежний файл не завершился вовремя; принудительная остановка не выполнялась",
    low_disk_suspended: "запись приостановлена из-за нехватки места",
    recorder_unavailable: "ожидаем службу записи",
    auth_failed: "камера отклонила логин или пароль",
    network_timeout: "камера не отвечает",
    storage_unavailable: "хранилище недоступно",
    permission_denied: "нет доступа к хранилищу",
    recording_not_ready: "поступление видео пока не подтверждено; проверьте состояние камеры",
    choiceTitle: "Как применить новый формат?",
    choiceMessage: "Камеры уже записывают видео. Выберите, когда перейти с {from} на {to}.",
    afterLabel: "После текущих файлов",
    afterDescription: "Каждая камера закончит свой файл и затем перейдёт на новый формат.",
    nowLabel: "Применить сейчас",
    nowDescription: "Аккуратно закончить текущие файлы и перезапустить запись. Возможен короткий разрыв.",
    safeNote: "Старые записи не переделываются и не удаляются. Во время перехода форматы могут временно различаться.",
    cancel: "Отмена",
    applying: "Применение…",
    choiceRequired: "Камеры записывают видео. Выберите способ применения нового формата.",
    stale: "Формат уже изменён в другой вкладке. Обновите настройки и повторите выбор.",
    invalid: "Не удалось принять выбор формата. Обновите настройки и повторите попытку.",
    saveErrorTitle: "Формат записи не изменён",
  },
  en: {
    title: "Recording format application",
    idle: "While cameras are recording, you can wait for current files or apply the format now.",
    pending: "Choice saved. Waiting for cameras to switch to {format}.",
    applied: "Recording in {format} is confirmed on all enabled cameras.",
    configured: "Setting saved. New recordings will use {format}.",
    failed: "The switch to {format} is not confirmed for every camera. Open details to see why.",
    details: "Details",
    close: "Close",
    waiting_segment: "waiting for the current file to finish",
    switching: "finishing the previous file and switching recording",
    starting: "starting recording and waiting for video",
    waiting: "waiting for recording to resume",
    appliedCamera: "new format confirmed",
    previous_segment_not_finalized: "the previous file was not confirmed as finalized; saved video was not deleted",
    finish_timeout: "the previous file did not finish in time; no forced stop was performed",
    low_disk_suspended: "recording is paused due to low disk space",
    recorder_unavailable: "waiting for the recording service",
    auth_failed: "the camera rejected the username or password",
    network_timeout: "the camera is not responding",
    storage_unavailable: "storage is unavailable",
    permission_denied: "storage access is denied",
    recording_not_ready: "video input is not confirmed yet; check the camera status",
    choiceTitle: "When should the new format apply?",
    choiceMessage: "Cameras are recording. Choose when to switch from {from} to {to}.",
    afterLabel: "After current files",
    afterDescription: "Each camera will finish its own file and then switch to the new format.",
    nowLabel: "Apply now",
    nowDescription: "Gracefully finish current files and restart recording. A short gap is possible.",
    safeNote: "Existing recordings are not converted or deleted. Formats may differ temporarily during the switch.",
    cancel: "Cancel",
    applying: "Applying…",
    choiceRequired: "Cameras are recording. Choose how to apply the new format.",
    stale: "The format changed in another tab. Reload settings and choose again.",
    invalid: "The recording format choice could not be accepted. Reload settings and try again.",
    saveErrorTitle: "Recording format was not changed",
  },
  "zh-CN": {
    title: "录像格式应用状态",
    idle: "摄像机正在录像时，可以等待当前文件完成，或立即应用新格式。",
    pending: "选择已保存，正在等待摄像机切换为 {format}。",
    applied: "所有已启用摄像机均已确认使用 {format} 录像。",
    configured: "设置已保存，新录像将使用 {format}。",
    failed: "部分摄像机尚未确认切换为 {format}，请打开详情查看原因。",
    details: "查看详情",
    close: "关闭",
    waiting_segment: "等待当前文件完成",
    switching: "正在完成旧文件并切换录像格式",
    starting: "正在启动录像并等待视频",
    waiting: "等待恢复录像",
    appliedCamera: "新格式已确认",
    previous_segment_not_finalized: "尚未确认旧文件完成，已保存的视频未被删除",
    finish_timeout: "旧文件未及时结束，未强制终止录像进程",
    low_disk_suspended: "磁盘空间不足，录像已暂停",
    recorder_unavailable: "等待录像服务",
    auth_failed: "摄像机拒绝了用户名或密码",
    network_timeout: "摄像机无响应",
    storage_unavailable: "存储不可用",
    permission_denied: "无法访问存储",
    recording_not_ready: "尚未确认视频输入，请检查摄像机状态",
    choiceTitle: "何时应用新格式？",
    choiceMessage: "摄像机正在录像。请选择何时从 {from} 切换为 {to}。",
    afterLabel: "当前文件完成后",
    afterDescription: "每台摄像机完成自己的当前文件后，切换到新格式。",
    nowLabel: "立即应用",
    nowDescription: "正常结束当前文件并重新启动录像，可能出现短暂间隔。",
    safeNote: "旧录像不会被转换或删除。切换期间可能暂时使用不同格式。",
    cancel: "取消",
    applying: "正在应用…",
    choiceRequired: "摄像机正在录像，请选择新格式的应用方式。",
    stale: "格式已在其他标签页更改，请刷新设置后重新选择。",
    invalid: "无法接受录像格式选择，请刷新设置后重试。",
    saveErrorTitle: "录像格式未更改",
  },
};

export function recordingFormatCopy(lang) {
  return COPY[lang] || COPY.ru;
}

export function recordingFormatError(error, lang) {
  const code = error?.detail?.error || error?.code || "";
  const copy = recordingFormatCopy(lang);
  if (["recording_format_change_requires_choice", "recording_format_change_blocked_active_recordings"].includes(code)) {
    return { kind: "choice", message: copy.choiceRequired };
  }
  if (code === "recording_format_settings_changed") return { kind: "stale", message: copy.stale };
  if (["recording_format_choice_invalid", "recording_format_target_required"].includes(code)) return { kind: "invalid", message: copy.invalid };
  return null;
}

export function recordingFormatNotice(status, lang) {
  const copy = recordingFormatCopy(lang);
  const state = ["applied", "pending", "failed"].includes(status?.state) ? status.state : "idle";
  const key = state === "applied" && !status?.items?.length ? "configured" : state;
  return { state, message: copy[key].replace("{format}", String(status?.target_format || "").toUpperCase()) };
}

export function recordingFormatCameraText(item, lang) {
  const copy = recordingFormatCopy(lang);
  if (item?.reason_code) return copy[item.reason_code] || copy.recording_not_ready;
  return copy[item?.state === "applied" ? "appliedCamera" : item?.state] || copy.recording_not_ready;
}

export function shouldWatchRecordingFormat(status) {
  return Boolean(status?.requested_at && ["pending", "failed"].includes(status.state));
}

export function recordingFormatNotificationKey(status) {
  if (!status?.requested_at || !["pending", "failed", "applied"].includes(status.state)) return "";
  const failures = status.state === "failed"
    ? (status.items || []).filter((item) => item.state === "failed").map((item) => [item.camera_id, item.reason_code || ""]).sort((a, b) => String(a[0]).localeCompare(String(b[0])))
    : [];
  return JSON.stringify([status.requested_at, status.target_format, status.state, failures]);
}
