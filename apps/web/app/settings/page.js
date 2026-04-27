"use client";

import { useEffect, useMemo, useState } from "react";
import Layout from "../../components/Layout";
import { apiFetch } from "../../lib/api";

const TIMEZONES = [
  "UTC",
  "Europe/Moscow",
  "Asia/Yekaterinburg",
  "Asia/Novosibirsk",
  "Asia/Krasnoyarsk",
  "Asia/Irkutsk",
  "Asia/Yakutsk",
  "Asia/Vladivostok",
];

const BACKEND_LABELS = {
  auto: { ru: "Автоматически", en: "Automatic" },
  qsv: { ru: "Intel Quick Sync", en: "Intel Quick Sync" },
  vaapi: { ru: "VAAPI / Linux hardware acceleration", en: "VAAPI / Linux hardware acceleration" },
  nvenc: { ru: "NVIDIA NVENC", en: "NVIDIA NVENC" },
  cpu: { ru: "CPU fallback", en: "CPU fallback" },
};

const TEXT = {
  ru: {
    title: "Настройки",
    subtitle: "Системные параметры KM VMS: язык, время, архив, запись, ускорение и безопасность.",
    save: "Сохранить",
    saving: "Сохранение...",
    saved: "Настройки сохранены",
    system: "Система",
    systemText: "Базовые параметры интерфейса и отображения времени.",
    language: "Язык",
    russian: "Русский",
    english: "English",
    timezone: "Часовой пояс",
    timezoneHelp: "Часовой пояс используется для отображения времени, архива и будущей хронологии.",
    storage: "Хранилище",
    storageText: "Папка архива внутри контейнера. Она должна быть подключена к папке на NAS/сервере через docker-compose volume.",
    storagePath: "Папка архива внутри контейнера",
    hostPath: "NAS/host path определяется в docker-compose volume mapping.",
    validate: "Проверить хранилище",
    storageOk: "Хранилище доступно.",
    storageFail: "Хранилище недоступно",
    path: "Путь",
    free: "Свободно",
    writeAllowed: "Запись разрешена",
    writeDenied: "Нет прав на запись",
    created: "Папка создана",
    exists: "Папка существует",
    recording: "Запись",
    recordingText: "Профиль задаёт формат будущих архивных файлов Recorder PRO.",
    recordingProfile: "Профиль записи",
    balanced: "Сбалансированный",
    balancedHelp: "Рекомендуемый режим. Сейчас сохраняет архив в MKV как наиболее безопасный общий вариант.",
    compatibility: "Максимальная совместимость",
    compatibilityHelp: "MP4 легче открыть во многих плеерах, но он менее устойчив при аварийном завершении записи.",
    reliability: "Максимальная надёжность",
    reliabilityHelp: "MKV лучше переносит прерывание процесса или сервера и подходит для NAS.",
    mapsTo: "Формат",
    hardware: "Аппаратное ускорение",
    hardwareText: "Показывает доступные возможности сервера и выбранный режим кодирования.",
    hardwareMode: "Режим аппаратного ускорения",
    rescan: "Проверить аппаратные возможности",
    hardwareAvailable: "Аппаратное ускорение доступно.",
    hardwareUnavailable: "Аппаратное ускорение недоступно. Будет использоваться CPU fallback.",
    dockerOk: "Docker имеет доступ к видеоустройству.",
    dockerFail: "Docker не имеет доступа к видеоустройству или устройство не найдено.",
    selected: "Выбрано",
    availableOptions: "Доступные варианты",
    noOptions: "На этом сервере аппаратные варианты недоступны или не прошли проверку.",
    cpuFallback: "Fallback на CPU доступен.",
    technicalDetails: "Технические детали",
    security: "Безопасность и сессия",
    securityText: "Если включить “Оставаться в системе”, вход сохраняется до 24:00 системного дня. После полуночи потребуется войти снова.",
    users: "Пользователи и роли",
    usersText: "Фундамент ролей уже включён: admin, operator, viewer. Полное управление пользователями будет добавлено отдельным этапом.",
    notAuthenticated: "Нужно войти заново.",
    forbidden: "Недостаточно прав.",
    network: "Сервер недоступен.",
  },
  en: {
    title: "Settings",
    subtitle: "KM VMS system settings: language, time, archive storage, recording, acceleration, and security.",
    save: "Save",
    saving: "Saving...",
    saved: "Settings saved",
    system: "System",
    systemText: "Core interface and displayed time settings.",
    language: "Language",
    russian: "Русский",
    english: "English",
    timezone: "Timezone",
    timezoneHelp: "Timezone is used for displayed time, archive timestamps, and future chronology.",
    storage: "Storage",
    storageText: "Archive folder inside the container. It must be mapped to a NAS/server folder through docker-compose volume mapping.",
    storagePath: "Archive folder inside container",
    hostPath: "NAS/host path is defined by docker-compose volume mapping.",
    validate: "Validate storage",
    storageOk: "Storage is available.",
    storageFail: "Storage is unavailable",
    path: "Path",
    free: "Free space",
    writeAllowed: "Write access: allowed",
    writeDenied: "Write access: denied",
    created: "Folder was created",
    exists: "Folder exists",
    recording: "Recording",
    recordingText: "The profile defines the future Recorder PRO archive file format.",
    recordingProfile: "Recording profile",
    balanced: "Balanced",
    balancedHelp: "Recommended mode. It currently stores archive files as MKV for the safest general-purpose behavior.",
    compatibility: "Maximum compatibility",
    compatibilityHelp: "MP4 opens more easily in many players, but is less crash-safe if recording stops abruptly.",
    reliability: "Maximum reliability",
    reliabilityHelp: "MKV is more resilient to interrupted processes or server shutdowns and suits NAS recording.",
    mapsTo: "Format",
    hardware: "Hardware Acceleration",
    hardwareText: "Shows server capabilities and the selected encoding mode.",
    hardwareMode: "Hardware acceleration mode",
    rescan: "Check hardware capabilities",
    hardwareAvailable: "Hardware acceleration is available.",
    hardwareUnavailable: "Hardware acceleration is unavailable. CPU fallback will be used.",
    dockerOk: "Docker has access to the video device.",
    dockerFail: "Docker cannot access the video device or the device is missing.",
    selected: "Selected",
    availableOptions: "Available options",
    noOptions: "Hardware options are unavailable or did not pass validation on this server.",
    cpuFallback: "CPU fallback is available.",
    technicalDetails: "Technical details",
    security: "Security / Session",
    securityText: "If “Stay signed in” is enabled, the session is kept until midnight of the system day. After midnight, login is required again.",
    users: "Users / Roles",
    usersText: "Role foundation is active: admin, operator, viewer. Full user management will be added in a dedicated stage.",
    notAuthenticated: "Please sign in again.",
    forbidden: "Insufficient permissions.",
    network: "Server is unavailable.",
  },
};

function languageOf(settings) {
  return settings?.language === "en" ? "en" : "ru";
}

function backendLabel(value, lang) {
  const key = value || "auto";
  return BACKEND_LABELS[key]?.[lang] || key;
}

function formatBytes(value, lang) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  const units = ["B", "GB", "TB"];
  if (bytes < 1024 ** 3) return `${Math.round(bytes / 1024 / 1024)} MB`;
  if (bytes < 1024 ** 4) return `${(bytes / 1024 ** 3).toFixed(1)} ${units[1]}`;
  return `${(bytes / 1024 ** 4).toFixed(1)} ${units[2]}`;
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

export default function SettingsPage() {
  const [settings, setSettings] = useState(null);
  const [hardware, setHardware] = useState(null);
  const [storageResult, setStorageResult] = useState(null);
  const [recordingProfile, setRecordingProfile] = useState("balanced");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const lang = languageOf(settings);
  const t = TEXT[lang] || TEXT.ru;
  const languageIcon = lang === "en"
    ? "/icons/nav/language-icon_ENG.png"
    : "/icons/nav/language-icon_RU.png";

  useEffect(() => {
    load();
  }, []);

  async function load() {
    setError("");
    try {
      const [settingsData, hardwareData] = await Promise.all([
        apiFetch("/settings"),
        apiFetch("/hardware/capabilities"),
      ]);
      setSettings(settingsData);
      setRecordingProfile(profileFromFormat(settingsData?.recording_format));
      setHardware(hardwareData);
    } catch (err) {
      setError(humanError(err, lang));
    }
  }

  function patch(key, value) {
    setSettings((current) => ({ ...current, [key]: value }));
  }

  async function save() {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const updated = await apiFetch("/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          timezone: settings.timezone,
          language: settings.language,
          storage_path: settings.storage_path,
          recording_format: recordingFormatForProfile(recordingProfile),
          hardware_preferred_backend: settings.hardware_preferred_backend || null,
        }),
      });
      setSettings(updated);
      setMessage(t.saved);
    } catch (err) {
      setError(humanError(err, lang));
    } finally {
      setBusy(false);
    }
  }

  async function validateStorage() {
    setStorageResult(null);
    setError("");
    setMessage("");
    try {
      const result = await apiFetch("/settings/storage/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ storage_path: settings.storage_path, create: true }),
      });
      setStorageResult(result);
      if (!result.ok) setError(storageMessage(result, lang));
    } catch (err) {
      setError(humanError(err, lang));
    }
  }

  async function rescanHardware() {
    setError("");
    setMessage("");
    try {
      setHardware(await apiFetch("/hardware/rescan", { method: "POST" }));
    } catch (err) {
      setError(humanError(err, lang));
    }
  }

  const hardwareOptions = useMemo(() => {
    const values = new Set(["auto", "cpu"]);
    if (settings?.hardware_preferred_backend) values.add(settings.hardware_preferred_backend);
    (hardware?.available_backends || []).forEach((item) => values.add(item));
    Object.entries(hardware?.backend_status || {}).forEach(([key, value]) => {
      if (value?.candidate) values.add(key);
    });
    return ["auto", "qsv", "vaapi", "nvenc", "cpu"].filter((item) => values.has(item));
  }, [hardware, settings?.hardware_preferred_backend]);

  const selectedHardware = settings?.hardware_preferred_backend || hardware?.selected_backend || "auto";
  const availableHardware = hardware?.available_backends || [];
  const profileHelp = {
    balanced: t.balancedHelp,
    compatibility: t.compatibilityHelp,
    reliability: t.reliabilityHelp,
  }[recordingProfile];

  return (
    <Layout>
      <div className="settingsPage">
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

        {error ? <div className="settingsAlert error">{error}</div> : null}
        {message ? <div className="settingsAlert ok">{message}</div> : null}

        {!settings ? null : (
          <div className="settingsSections">
            <section className="settingsSection">
              <div className="settingsSectionHead">
                <h2 className="settingsSectionTitle">
                  <img src="/icons/nav/settings-icon.png" alt="" />
                  <span>{t.system}</span>
                </h2>
                <p>{t.systemText}</p>
              </div>
              <div className="settingsGrid">
                <label className="settingsField">
                  <span className="settingsFieldLabel">
                    <img src={languageIcon} alt="" />
                    {t.language}
                  </span>
                  <select className="select" value={settings.language} onChange={(event) => patch("language", event.target.value)}>
                    <option value="ru">{t.russian}</option>
                    <option value="en">{t.english}</option>
                  </select>
                </label>
                <label className="settingsField">
                  <span className="settingsFieldLabel">
                    <img src="/icons/nav/timezone-icon.png" alt="" />
                    {t.timezone}
                  </span>
                  <select className="select" value={settings.timezone || ""} onChange={(event) => patch("timezone", event.target.value)}>
                    {TIMEZONES.map((zone) => <option key={zone} value={zone}>{zone}</option>)}
                  </select>
                  <span className="settingsHint">{t.timezoneHelp}</span>
                </label>
              </div>
            </section>

            <section className="settingsSection">
              <div className="settingsSectionHead">
                <h2 className="settingsSectionTitle">
                  <img src="/icons/nav/storage-icon.png" alt="" />
                  <span>{t.storage}</span>
                </h2>
                <p>{t.storageText}</p>
              </div>
              <div className="settingsGrid">
                <label className="settingsField settingsFull">
                  <span>{t.storagePath}</span>
                  <input className="input" value={settings.storage_path || ""} onChange={(event) => patch("storage_path", event.target.value)} />
                  <span className="settingsHint">{t.hostPath}</span>
                </label>
              </div>
              <div className="settingsActions">
                <button className="button secondary small" onClick={validateStorage}>{t.validate}</button>
                {storageResult ? (
                  <div className={`settingsStatus ${storageResult.ok ? "ok" : "error"}`}>
                    <strong>{storageMessage(storageResult, lang)}</strong>
                    <span>{t.path}: {storageResult.path}</span>
                    <span>{storageResult.writable ? t.writeAllowed : t.writeDenied}</span>
                    <span>{storageResult.created ? t.created : t.exists}: {storageResult.exists ? "yes" : "no"}</span>
                    {storageResult.free_bytes != null ? <span>{t.free}: {formatBytes(storageResult.free_bytes, lang)}</span> : null}
                  </div>
                ) : null}
              </div>
            </section>

            <section className="settingsSection">
              <div className="settingsSectionHead">
                <h2 className="settingsSectionTitle">
                  <img src="/icons/nav/records.png" alt="" />
                  <span>{t.recording}</span>
                </h2>
                <p>{t.recordingText}</p>
              </div>
              <label className="settingsField">
                <span>{t.recordingProfile}</span>
                <select className="select" value={recordingProfile} onChange={(event) => setRecordingProfile(event.target.value)}>
                  <option value="balanced">{t.balanced}</option>
                  <option value="compatibility">{t.compatibility}</option>
                  <option value="reliability">{t.reliability}</option>
                </select>
              </label>
              <div className="settingsNote">
                {profileHelp} {t.mapsTo}: {recordingFormatForProfile(recordingProfile).toUpperCase()}.
              </div>
            </section>

            <section className="settingsSection">
              <div className="settingsSectionHead">
                <h2 className="settingsSectionTitle">
                  <img src="/icons/nav/hardware-icon.png" alt="" />
                  <span>{t.hardware}</span>
                </h2>
                <p>{t.hardwareText}</p>
              </div>
              <div className="settingsGrid settingsHardwareGrid">
                <label className="settingsField">
                  <span>{t.hardwareMode}</span>
                  <select className="select" value={settings.hardware_preferred_backend || ""} onChange={(event) => patch("hardware_preferred_backend", event.target.value || null)}>
                    {hardwareOptions.map((backend) => (
                      <option key={backend} value={backend === "auto" ? "" : backend} title={backendLabel(backend, lang)}>
                        {backendLabel(backend, lang)}
                      </option>
                    ))}
                  </select>
                </label>
                <div className={`settingsStatus ${hardware?.hardware_accel_available ? "ok" : "warn"}`}>
                  <strong>{hardware?.hardware_accel_available ? t.hardwareAvailable : t.hardwareUnavailable}</strong>
                  <span>{t.selected}: {backendLabel(selectedHardware, lang)}</span>
                  <span>{hardware?.docker_device_access_ok ? t.dockerOk : t.dockerFail}</span>
                  <span>{t.cpuFallback}</span>
                </div>
              </div>
              <div className="settingsActions">
                <button className="button secondary small" onClick={rescanHardware}>{t.rescan}</button>
              </div>
              <div className="settingsNote">
                {availableHardware.length
                  ? `${t.availableOptions}: ${availableHardware.map((backend) => backendLabel(backend, lang)).join(", ")}.`
                  : t.noOptions}
              </div>
              {[...(hardware?.warnings || []), ...(hardware?.errors || [])].length ? (
                <div className="settingsStatus warn compact">
                  {[...(hardware?.warnings || []), ...(hardware?.errors || [])].map((item, index) => (
                    <span key={`${item}-${index}`}>{item}</span>
                  ))}
                </div>
              ) : null}
              {hardware ? (
                <details className="settingsDetails">
                  <summary>{t.technicalDetails}</summary>
                  <pre className="settingsJson">{JSON.stringify(hardware, null, 2)}</pre>
                </details>
              ) : null}
            </section>

            <section className="settingsSection">
              <div className="settingsSectionHead">
                <h2 className="settingsSectionTitle">
                  <img src="/icons/nav/security-icon.png" alt="" />
                  <span>{t.security}</span>
                </h2>
                <p>{t.securityText}</p>
              </div>
              <div className="settingsUsersNote">
                <img src="/icons/nav/users-icon.png" alt="" />
                <div>
                  <strong>{t.users}</strong>
                  <span>{t.usersText}</span>
                </div>
              </div>
            </section>
          </div>
        )}
      </div>
    </Layout>
  );
}
