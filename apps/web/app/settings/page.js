"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Layout from "../../components/Layout";
import { apiFetch } from "../../lib/api";

const UTC_TIMEZONES = Array.from({ length: 27 }, (_, index) => {
  const offset = index - 12;
  const sign = offset >= 0 ? "+" : "-";
  const label = offset === 0 ? "UTC+00:00" : `UTC${sign}${String(Math.abs(offset)).padStart(2, "0")}:00`;
  const value = offset === 0 ? "UTC" : `Etc/GMT${offset > 0 ? "-" : "+"}${Math.abs(offset)}`;
  return { offset, label, value };
});

const HARDWARE_OPTIONS = ["auto", "qsv", "vaapi", "amf", "nvenc", "cpu"];

const BACKEND_LABELS = {
  auto: { ru: "Автоматически", en: "Automatic" },
  qsv: { ru: "Intel Quick Sync / QSV", en: "Intel Quick Sync / QSV" },
  vaapi: { ru: "VAAPI", en: "VAAPI" },
  amf: { ru: "AMD AMF", en: "AMD AMF" },
  nvenc: { ru: "NVIDIA NVENC/NVDEC", en: "NVIDIA NVENC/NVDEC" },
  cpu: { ru: "Резервный режим CPU", en: "CPU fallback" },
};

const TEXT = {
  ru: {
    title: "Настройки",
    subtitle: "Системные параметры KM VMS: язык, время, архив, запись, ускорение и безопасность.",
    save: "Сохранить",
    saving: "Сохранение...",
    saved: "Настройки сохранены",
    changesApplied: "Изменения успешно применены",
    storageValidated: "Хранилище доступно. Свободно: {free}",
    hardwareChecked: "Аппаратные возможности проверены",
    language: "Язык",
    russian: "Русский",
    english: "English",
    timezone: "Часовой пояс",
    timezoneHelp: "Определяет время интерфейса, архива и хронологии.",
    storage: "Хранилище",
    storageText: "Путь архива внутри контейнера. Серверный путь задаётся в docker-compose.",
    hostPath: "Путь на сервере",
    hostPathUnknown: "Определяется в docker-compose",
    validate: "Тест",
    storageOk: "Хранилище доступно.",
    storageFail: "Хранилище недоступно",
    writeDenied: "Нет прав на запись",
    recording: "Запись",
    balanced: "Баланс",
    balancedHelp: "Рекомендуемый режим. Сейчас используется MKV.",
    compatibility: "Макс. совместимость",
    compatibilityHelp: "Сейчас используется MP4. Удобнее для плееров.",
    reliability: "Макс. надежность",
    reliabilityHelp: "Сейчас используется MKV. Лучше переносит сбои записи.",
    mapsTo: "Формат",
    hardware: "Аппаратное ускорение",
    hardwareText: "Доступные режимы сервера. Недоступные варианты показаны серым.",
    unavailableMode: "Этот режим недоступен на данном сервере или не прошёл проверку.",
    rescan: "Тест",
    hardwareAvailable: "Аппаратное ускорение доступно.",
    hardwareUnavailable: "Аппаратное ускорение недоступно. Будет использоваться CPU fallback.",
    selected: "Выбрано",
    failedValidation: "Не прошёл проверку",
    notDetected: "Не найдено на этом сервере",
    security: "Безопасность и сессия",
    securityText: "Если включить «Оставаться в системе», вход сохраняется до 24:00 системного дня.",
    users: "Пользователи и роли",
    usersText: "Фундамент ролей уже включён: admin, operator, viewer. Полное управление пользователями будет добавлено отдельным этапом.",
    notAuthenticated: "Нужно войти заново.",
    forbidden: "Недостаточно прав.",
    network: "Сервер недоступен.",
    i18nNote: "Язык применяется к странице настроек и общим элементам интерфейса.",
    tooltips: {
      timezone: "Используется для отображения времени, записи файлов и хронологии.",
      storage: "Папка внутри контейнера. Фактический путь на сервере задаётся в docker-compose.",
      recording: "Определяет формат и поведение записи видео.",
      hardware: "Использует видеоускорение сервера для обработки видео.",
      auto: "Система сама выбирает оптимальное ускорение.",
      qsv: "Аппаратное ускорение через встроенную графику Intel.",
      vaapi: "Аппаратное ускорение для Linux-систем.",
      nvenc: "Аппаратное ускорение через видеокарты NVIDIA.",
      amf: "Аппаратное ускорение через видеокарты AMD.",
      cpu: "Обработка видео без ускорения, только на процессоре.",
      security: "Настройки авторизации и времени сессии.",
      users: "Управление доступом пользователей к системе.",
    },
  },
  en: {
    title: "Settings",
    subtitle: "KM VMS system settings: language, time, archive storage, recording, acceleration, and security.",
    save: "Save",
    saving: "Saving...",
    saved: "Settings saved",
    changesApplied: "Changes applied successfully",
    storageValidated: "Storage is available. Free: {free}",
    hardwareChecked: "Hardware capabilities checked",
    language: "Language",
    russian: "Русский",
    english: "English",
    timezone: "Timezone",
    timezoneHelp: "Defines interface time, archive timestamps, and chronology.",
    storage: "Storage",
    storageText: "Archive path inside the container. The server path is defined in docker-compose.",
    hostPath: "Server path",
    hostPathUnknown: "Defined in docker-compose",
    validate: "Test",
    storageOk: "Storage is available.",
    storageFail: "Storage is unavailable",
    writeDenied: "Write access denied",
    recording: "Recording",
    balanced: "Balanced",
    balancedHelp: "Recommended mode. Current mapping: MKV.",
    compatibility: "Maximum compatibility",
    compatibilityHelp: "Current mapping: MP4. Easier to open in players.",
    reliability: "Maximum reliability",
    reliabilityHelp: "Current mapping: MKV. More resilient to recording interruptions.",
    mapsTo: "Format",
    hardware: "Hardware Acceleration",
    hardwareText: "Server acceleration modes. Unavailable options are visible but disabled.",
    unavailableMode: "This mode is unavailable on this server or failed validation.",
    rescan: "Test",
    hardwareAvailable: "Hardware acceleration is available.",
    hardwareUnavailable: "Hardware acceleration is unavailable. CPU fallback will be used.",
    selected: "Selected",
    failedValidation: "Failed validation",
    notDetected: "Not detected on this server",
    security: "Security / Session",
    securityText: "If \"Stay signed in\" is enabled, the session is kept until midnight of the system day.",
    users: "Users / Roles",
    usersText: "Role foundation is active: admin, operator, viewer. Full user management will be added in a dedicated stage.",
    notAuthenticated: "Please sign in again.",
    forbidden: "Insufficient permissions.",
    network: "Server is unavailable.",
    i18nNote: "Language is applied to Settings and shared interface controls.",
    tooltips: {
      timezone: "Used for time display, recording timestamps, and chronology.",
      storage: "Folder inside the container. The real server path is defined in docker-compose.",
      recording: "Defines recording format and behavior.",
      hardware: "Uses server video acceleration for video processing.",
      auto: "The system selects the best available acceleration.",
      qsv: "Hardware acceleration through Intel integrated graphics.",
      vaapi: "Hardware acceleration for Linux systems.",
      nvenc: "Hardware acceleration through NVIDIA GPUs.",
      amf: "Hardware acceleration through AMD GPUs.",
      cpu: "Video processing without acceleration, using CPU only.",
      security: "Authorization and session time settings.",
      users: "Manage user access to the system.",
    },
  },
};

function languageOf(settings) {
  return settings?.language === "en" ? "en" : "ru";
}

function backendLabel(value, lang) {
  const key = value || "auto";
  return BACKEND_LABELS[key]?.[lang] || key;
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "-";
  if (bytes < 1024 ** 3) return `${Math.round(bytes / 1024 / 1024)} MB`;
  if (bytes < 1024 ** 4) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  return `${(bytes / 1024 ** 4).toFixed(1)} TB`;
}

function parseErrorDetail(message) {
  if (!message) return null;
  try {
    return JSON.parse(message);
  } catch {
    return null;
  }
}

function humanError(err, lang) {
  const t = TEXT[lang] || TEXT.ru;
  const message = err?.message || "";
  const detail = parseErrorDetail(message);
  const storage = detail?.storage || detail?.detail?.storage;

  if (storage?.error) return `${t.storageFail}: ${storage.error}`;
  if (message.includes("Not authenticated") || message.includes("401")) return t.notAuthenticated;
  if (message.includes("403") || message.includes("Forbidden")) return t.forbidden;
  if (message.includes("Failed to fetch") || message.includes("NetworkError")) return t.network;
  return message || t.network;
}

function storageMessage(result, lang) {
  if (!result) return null;
  const t = TEXT[lang] || TEXT.ru;
  if (!result.ok) return `${t.storageFail}: ${result.error || t.writeDenied}`;
  return t.storageOk;
}

function recordingFormatForProfile(profile) {
  return profile === "compatibility" ? "mp4" : "mkv";
}

function profileFromFormat(format) {
  return format === "mp4" ? "compatibility" : "balanced";
}

function offsetFromTimezone(timezone) {
  try {
    const value = new Intl.DateTimeFormat("en-US", {
      timeZone: timezone,
      timeZoneName: "shortOffset",
      hour: "2-digit",
    }).formatToParts(new Date()).find((part) => part.type === "timeZoneName")?.value || "GMT";
    const match = value.match(/^GMT([+-])(\d{1,2})(?::\d{2})?$/);
    if (!match) return 0;
    return (match[1] === "-" ? -1 : 1) * Number(match[2]);
  } catch {
    return 0;
  }
}

function timezoneValueForSettings(timezone) {
  if (UTC_TIMEZONES.some((zone) => zone.value === timezone)) return timezone;
  const offset = offsetFromTimezone(timezone);
  return UTC_TIMEZONES.find((zone) => zone.offset === offset)?.value || "UTC";
}

function hardwareOptionState(backend, hardware, t) {
  if (backend === "auto" || backend === "cpu") return { selectable: true, reason: "" };
  const status = hardware?.backend_status?.[backend];
  const available = (hardware?.available_backends || []).includes(backend);
  if (available) return { selectable: true, reason: "" };
  if (status?.candidate) return { selectable: false, reason: status.reason || t.failedValidation };
  return { selectable: false, reason: t.notDetected };
}

function InfoTip({ text }) {
  if (!text) return null;
  return (
    <span className="settingsInfoTip" tabIndex="0" aria-label={text}>
      i
      <span className="settingsInfoBubble" role="tooltip">{text}</span>
    </span>
  );
}

export default function SettingsPage() {
  const [settings, setSettings] = useState(null);
  const [hardware, setHardware] = useState(null);
  const [recordingProfile, setRecordingProfile] = useState("balanced");
  const [toast, setToast] = useState(null);
  const [busy, setBusy] = useState(false);
  const toastTimerRef = useRef(null);
  const lang = languageOf(settings);
  const t = TEXT[lang] || TEXT.ru;
  const languageIcon = lang === "en"
    ? "/icons/nav/language-icon_ENG.png"
    : "/icons/nav/language-icon_RU.png";

  useEffect(() => {
    load();
    function onLanguage(event) {
      if (event.detail) patch("language", event.detail);
    }
    window.addEventListener("km-vms-language", onLanguage);
    return () => {
      window.clearTimeout(toastTimerRef.current);
      window.removeEventListener("km-vms-language", onLanguage);
    };
  }, []);

  function showToast(title, type = "ok", subtitle = "") {
    setToast({ title, subtitle, type });
    window.clearTimeout(toastTimerRef.current);
    toastTimerRef.current = window.setTimeout(() => setToast(null), 2800);
  }

  async function load() {
    try {
      const [settingsData, hardwareData] = await Promise.all([
        apiFetch("/settings"),
        apiFetch("/hardware/capabilities"),
      ]);
      setSettings(settingsData);
      setRecordingProfile(profileFromFormat(settingsData?.recording_format));
      setHardware(hardwareData);
    } catch (err) {
      showToast(humanError(err, lang), "error");
    }
  }

  function patch(key, value) {
    setSettings((current) => ({ ...current, [key]: value }));
  }

  async function save() {
    setBusy(true);
    try {
      const updated = await apiFetch("/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          timezone: timezoneValueForSettings(settings.timezone),
          language: settings.language,
          storage_path: settings.storage_path,
          recording_format: recordingFormatForProfile(recordingProfile),
          hardware_preferred_backend: settings.hardware_preferred_backend || null,
        }),
      });
      setSettings(updated);
      window.dispatchEvent(new CustomEvent("km-vms-language", { detail: updated.language }));
      showToast(t.saved, "ok", t.changesApplied);
    } catch (err) {
      showToast(humanError(err, lang), "error");
    } finally {
      setBusy(false);
    }
  }

  async function validateStorage() {
    try {
      const result = await apiFetch("/settings/storage/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ storage_path: settings.storage_path, create: true }),
      });
      showToast(
        result.ok ? t.storageValidated.replace("{free}", formatBytes(result.free_bytes)) : storageMessage(result, lang),
        result.ok ? "ok" : "error"
      );
    } catch (err) {
      showToast(humanError(err, lang), "error");
    }
  }

  async function rescanHardware() {
    try {
      setHardware(await apiFetch("/hardware/rescan", { method: "POST" }));
      showToast(t.hardwareChecked);
    } catch (err) {
      showToast(humanError(err, lang), "error");
    }
  }

  const selectedHardware = settings?.hardware_preferred_backend || "auto";
  const profileHelp = {
    balanced: t.balancedHelp,
    compatibility: t.compatibilityHelp,
    reliability: t.reliabilityHelp,
  }[recordingProfile];

  const hardwareSummary = useMemo(() => (
    HARDWARE_OPTIONS.map((backend) => ({
      backend,
      ...hardwareOptionState(backend, hardware, t),
    }))
  ), [hardware, t]);

  function handleHardwareChange(event) {
    const value = event.target.value || "auto";
    const state = hardwareOptionState(value, hardware, t);
    if (!state.selectable) {
      showToast(t.unavailableMode, "error");
      return;
    }
    patch("hardware_preferred_backend", value === "auto" ? null : value);
  }

  function handleSettingsLanguageChange(event) {
    const nextLanguage = event.target.value;
    patch("language", nextLanguage);
    window.dispatchEvent(new CustomEvent("km-vms-language", { detail: nextLanguage }));
  }

  return (
    <Layout>
      <div className="settingsPage">
        {toast ? (
          <div className={`settingsToast ${toast.type}`}>
            <strong>{toast.title}</strong>
            {toast.subtitle ? <span>{toast.subtitle}</span> : null}
          </div>
        ) : null}

        <div className="settingsWorkspace">
          <div className="pageHeader settingsHeader">
            <div className="settingsTitleBlock">
              <img src="/icons/nav/settings-icon.png" alt="" />
              <div>
                <h1 className="pageTitle">{t.title}</h1>
                <div className="pageSubtitle">{t.subtitle}</div>
              </div>
            </div>
            <button className="button small" onClick={save} disabled={!settings || busy}>
              {busy ? t.saving : t.save}
            </button>
          </div>

          {!settings ? null : (
            <div className="settingsReferenceLayout">
              <section className="settingsPanel">
                <div className="settingsRow">
                  <div className="settingsRowIcon"><img src={languageIcon} alt="" /></div>
                  <div className="settingsRowText">
                    <strong>{t.language}</strong>
                    <span>{t.i18nNote}</span>
                  </div>
                  <div className="settingsRowControl">
                    <select className="select settingsSelect" value={settings.language} onChange={handleSettingsLanguageChange}>
                      <option value="ru">{t.russian}</option>
                      <option value="en">{t.english}</option>
                    </select>
                  </div>
                </div>

                <div className="settingsRow">
                  <div className="settingsRowIcon"><img src="/icons/nav/timezone-icon.png" alt="" /></div>
                  <div className="settingsRowText">
                    <strong>{t.timezone}<InfoTip text={t.tooltips.timezone} /></strong>
                    <span>{t.timezoneHelp}</span>
                  </div>
                  <div className="settingsRowControl">
                    <select className="select settingsSelect timezoneSelect" value={timezoneValueForSettings(settings.timezone)} onChange={(event) => patch("timezone", event.target.value)}>
                      {UTC_TIMEZONES.map((zone) => <option key={zone.value} value={zone.value}>{zone.label}</option>)}
                    </select>
                  </div>
                </div>

                <div className="settingsRow">
                  <div className="settingsRowIcon"><img src="/icons/nav/storage-icon.png" alt="" /></div>
                  <div className="settingsRowText">
                    <strong>{t.storage}<InfoTip text={t.tooltips.storage} /></strong>
                    <span>{t.storageText}</span>
                    <small>{t.hostPath}: {settings.storage_host_path || t.hostPathUnknown}</small>
                  </div>
                  <div className="settingsRowControl">
                    <input className="input settingsInput" value={settings.storage_path || ""} onChange={(event) => patch("storage_path", event.target.value)} />
                    <button className="button secondary small settingsTestButton" onClick={validateStorage}>{t.validate}</button>
                  </div>
                </div>

                <div className="settingsRow">
                  <div className="settingsRowIcon"><img src="/icons/nav/records.png" alt="" /></div>
                  <div className="settingsRowText">
                    <strong>{t.recording}<InfoTip text={t.tooltips.recording} /></strong>
                    <span>{profileHelp} {t.mapsTo}: {recordingFormatForProfile(recordingProfile).toUpperCase()}.</span>
                  </div>
                  <div className="settingsRowControl">
                    <select className="select settingsSelect" value={recordingProfile} onChange={(event) => setRecordingProfile(event.target.value)}>
                      <option value="balanced">{t.balanced}</option>
                      <option value="reliability">{t.reliability}</option>
                      <option value="compatibility">{t.compatibility}</option>
                    </select>
                  </div>
                </div>

                <div className="settingsRow settingsRowHardware">
                  <div className="settingsRowIcon"><img src="/icons/nav/hardware-icon.png" alt="" /></div>
                  <div className="settingsRowText">
                    <strong>{t.hardware}<InfoTip text={t.tooltips.hardware} /></strong>
                    <span>{hardware?.hardware_accel_available ? t.hardwareAvailable : t.hardwareUnavailable} {t.selected}: {backendLabel(selectedHardware, lang)}.</span>
                    <div className="settingsHardwareOptions">
                      {hardwareSummary.map(({ backend, selectable, reason }) => (
                        <span key={backend} className={`settingsBadge settingsBadge-${backend} ${selectable ? "ok" : "disabled"}`} title={reason || t.tooltips[backend]}>
                          {backendLabel(backend, lang)}
                          <InfoTip text={t.tooltips[backend]} />
                        </span>
                      ))}
                    </div>
                  </div>
                  <div className="settingsRowControl">
                    <select className="select settingsSelect" value={selectedHardware} onChange={handleHardwareChange}>
                      {hardwareSummary.map(({ backend, selectable, reason }) => (
                        <option key={backend} value={backend} disabled={!selectable} title={reason || backendLabel(backend, lang)}>
                          {backendLabel(backend, lang)}
                        </option>
                      ))}
                    </select>
                    <button className="button secondary small settingsTestButton" onClick={rescanHardware}>{t.rescan}</button>
                  </div>
                </div>

                <div className="settingsRow">
                  <div className="settingsRowIcon"><img src="/icons/nav/security-icon.png" alt="" /></div>
                  <div className="settingsRowText">
                    <strong>{t.security}<InfoTip text={t.tooltips.security} /></strong>
                    <span>{t.securityText}</span>
                  </div>
                  <div className="settingsRowControl settingsRowControlMeta">
                    <span className="settingsMetaPill">24:00</span>
                  </div>
                </div>

                <div className="settingsRow">
                  <div className="settingsRowIcon"><img src="/icons/nav/users-icon.png" alt="" /></div>
                  <div className="settingsRowText">
                    <strong>{t.users}<InfoTip text={t.tooltips.users} /></strong>
                    <span>{t.usersText}</span>
                  </div>
                  <div className="settingsRowControl settingsRowControlMeta">
                    <img className="settingsInlineIcon" src="/icons/nav/users-icon.png" alt="" />
                  </div>
                </div>
              </section>
            </div>
          )}
        </div>
      </div>
    </Layout>
  );
}
