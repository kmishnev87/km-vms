"use client";

import { useEffect, useMemo, useState } from "react";
import Layout from "../../components/Layout";
import { apiFetch } from "../../lib/api";

const TIMEZONE_GROUPS = [
  { label: "UTC", zones: [{ value: "UTC", ru: "UTC", en: "UTC" }] },
  {
    label: "Europe",
    zones: [
      { value: "Europe/London", ru: "Лондон", en: "London" },
      { value: "Europe/Berlin", ru: "Берлин", en: "Berlin" },
      { value: "Europe/Paris", ru: "Париж", en: "Paris" },
      { value: "Europe/Moscow", ru: "Москва", en: "Moscow" },
    ],
  },
  {
    label: "Asia",
    zones: [
      { value: "Asia/Yekaterinburg", ru: "Екатеринбург", en: "Yekaterinburg" },
      { value: "Asia/Novosibirsk", ru: "Новосибирск", en: "Novosibirsk" },
      { value: "Asia/Krasnoyarsk", ru: "Красноярск", en: "Krasnoyarsk" },
      { value: "Asia/Irkutsk", ru: "Иркутск", en: "Irkutsk" },
      { value: "Asia/Yakutsk", ru: "Якутск", en: "Yakutsk" },
      { value: "Asia/Vladivostok", ru: "Владивосток", en: "Vladivostok" },
      { value: "Asia/Tokyo", ru: "Токио", en: "Tokyo" },
      { value: "Asia/Shanghai", ru: "Шанхай", en: "Shanghai" },
      { value: "Asia/Dubai", ru: "Дубай", en: "Dubai" },
    ],
  },
  {
    label: "North America",
    zones: [
      { value: "America/New_York", ru: "Eastern Time", en: "Eastern Time" },
      { value: "America/Chicago", ru: "Central Time", en: "Central Time" },
      { value: "America/Denver", ru: "Mountain Time", en: "Mountain Time" },
      { value: "America/Los_Angeles", ru: "Pacific Time", en: "Pacific Time" },
      { value: "America/Toronto", ru: "Торонто", en: "Toronto" },
    ],
  },
  {
    label: "South America",
    zones: [
      { value: "America/Sao_Paulo", ru: "Сан-Паулу", en: "Sao Paulo" },
      { value: "America/Argentina/Buenos_Aires", ru: "Буэнос-Айрес", en: "Buenos Aires" },
    ],
  },
  {
    label: "Australia",
    zones: [
      { value: "Australia/Sydney", ru: "Сидней", en: "Sydney" },
      { value: "Australia/Perth", ru: "Перт", en: "Perth" },
    ],
  },
];

const HARDWARE_OPTIONS = ["auto", "qsv", "vaapi", "nvenc", "amf", "cpu"];

const BACKEND_LABELS = {
  auto: { ru: "Автоматически", en: "Automatic" },
  qsv: { ru: "Intel Quick Sync / QSV", en: "Intel Quick Sync / QSV" },
  vaapi: { ru: "VAAPI / Linux hardware acceleration", en: "VAAPI / Linux hardware acceleration" },
  nvenc: { ru: "NVIDIA NVENC/NVDEC", en: "NVIDIA NVENC/NVDEC" },
  amf: { ru: "AMD AMF / AMD hardware acceleration", en: "AMD AMF / AMD hardware acceleration" },
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
    storageText: "Физический путь на NAS задаётся в docker-compose volume mapping. Внутри контейнера он доступен как путь архива ниже.",
    storagePath: "Путь архива внутри контейнера",
    hostPath: "NAS/server path определяется в docker-compose volume mapping.",
    validate: "Проверить хранилище",
    storageOk: "Хранилище доступно.",
    storageFail: "Хранилище недоступно",
    path: "Путь",
    free: "Свободно",
    writeAllowed: "Запись разрешена",
    writeDenied: "Нет прав на запись",
    created: "Папка создана",
    exists: "Папка существует",
    yes: "да",
    no: "нет",
    recording: "Запись",
    recordingText: "Профиль задаёт формат будущих архивных файлов Recorder PRO.",
    recordingProfile: "Профиль записи",
    balanced: "Сбалансированный",
    balancedHelp: "Рекомендуемый режим. Сейчас: MKV.",
    compatibility: "Максимальная совместимость",
    compatibilityHelp: "Сейчас: MP4. Легче открыть в плеерах, но менее устойчив при аварийном завершении.",
    reliability: "Максимальная надёжность",
    reliabilityHelp: "Сейчас: MKV. Лучше переносит прерывание процесса или сервера.",
    mapsTo: "Формат",
    hardware: "Аппаратное ускорение",
    hardwareText: "Доступные режимы сервера. Недоступные варианты видны, но отключены.",
    hardwareMode: "Режим аппаратного ускорения",
    unavailableMode: "Этот режим недоступен на данном сервере или не прошёл проверку.",
    rescan: "Проверить аппаратные возможности",
    hardwareAvailable: "Аппаратное ускорение доступно.",
    hardwareUnavailable: "Аппаратное ускорение недоступно. Будет использоваться CPU fallback.",
    dockerOk: "Docker имеет доступ к видеоустройству.",
    dockerFail: "Docker не имеет доступа к видеоустройству или устройство не найдено.",
    selected: "Выбрано",
    availableOptions: "Доступные варианты",
    failedValidation: "Не прошёл проверку",
    notDetected: "Не найдено на этом сервере",
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
    storageText: "The physical NAS path is defined by docker-compose volume mapping. Inside the container it is available as the archive path below.",
    storagePath: "Archive path inside container",
    hostPath: "NAS/server path is defined by docker-compose volume mapping.",
    validate: "Validate storage",
    storageOk: "Storage is available.",
    storageFail: "Storage is unavailable",
    path: "Path",
    free: "Free space",
    writeAllowed: "Write access: allowed",
    writeDenied: "Write access: denied",
    created: "Folder was created",
    exists: "Folder exists",
    yes: "yes",
    no: "no",
    recording: "Recording",
    recordingText: "The profile defines the future Recorder PRO archive file format.",
    recordingProfile: "Recording profile",
    balanced: "Balanced",
    balancedHelp: "Recommended mode. Current mapping: MKV.",
    compatibility: "Maximum compatibility",
    compatibilityHelp: "Current mapping: MP4. Easier to open in players, but less crash-safe.",
    reliability: "Maximum reliability",
    reliabilityHelp: "Current mapping: MKV. More resilient to process or server interruptions.",
    mapsTo: "Format",
    hardware: "Hardware Acceleration",
    hardwareText: "Server acceleration modes. Unavailable options are visible but disabled.",
    hardwareMode: "Hardware acceleration mode",
    unavailableMode: "This mode is unavailable on this server or failed validation.",
    rescan: "Check hardware capabilities",
    hardwareAvailable: "Hardware acceleration is available.",
    hardwareUnavailable: "Hardware acceleration is unavailable. CPU fallback will be used.",
    dockerOk: "Docker has access to the video device.",
    dockerFail: "Docker cannot access the video device or the device is missing.",
    selected: "Selected",
    availableOptions: "Available options",
    failedValidation: "Failed validation",
    notDetected: "Not detected on this server",
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

function timezoneOffset(zone) {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: zone,
      timeZoneName: "shortOffset",
      hour: "2-digit",
    }).formatToParts(new Date());
    const value = parts.find((part) => part.type === "timeZoneName")?.value || "UTC";
    return value.replace("GMT", "UTC");
  } catch {
    return "UTC";
  }
}

function timezoneLabel(zone, lang) {
  const name = zone[lang] || zone.en || zone.value;
  return `${timezoneOffset(zone.value)} — ${name} (${zone.value})`;
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
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

function hardwareOptionState(backend, hardware, t) {
  if (backend === "auto" || backend === "cpu") return { selectable: true, reason: "" };
  const status = hardware?.backend_status?.[backend];
  const available = (hardware?.available_backends || []).includes(backend);
  if (available) return { selectable: true, reason: "" };
  if (status?.candidate) return { selectable: false, reason: status.reason || t.failedValidation };
  return { selectable: false, reason: t.notDetected };
}

export default function SettingsPage() {
  const [settings, setSettings] = useState(null);
  const [hardware, setHardware] = useState(null);
  const [storageResult, setStorageResult] = useState(null);
  const [recordingProfile, setRecordingProfile] = useState("balanced");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [hardwareNotice, setHardwareNotice] = useState("");
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

  const selectedHardware = settings?.hardware_preferred_backend || "auto";
  const availableHardware = hardware?.available_backends || [];
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
      setHardwareNotice(t.unavailableMode);
      return;
    }
    setHardwareNotice("");
    patch("hardware_preferred_backend", value === "auto" ? null : value);
  }

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
        {hardwareNotice ? <div className="settingsAlert error">{hardwareNotice}</div> : null}

        {!settings ? null : (
          <div className="settingsSections">
            <section className="settingsSection settingsSectionCompact">
              <div className="settingsSectionHead">
                <h2 className="settingsSectionTitle">
                  <img src="/icons/nav/settings-icon.png" alt="" />
                  <span>{t.system}</span>
                </h2>
                <p>{t.systemText}</p>
              </div>
              <div className="settingsControlGrid two">
                <label className="settingsField settingsControl">
                  <span className="settingsFieldLabel">
                    <img src={languageIcon} alt="" />
                    {t.language}
                  </span>
                  <select className="select settingsSelect" value={settings.language} onChange={(event) => patch("language", event.target.value)}>
                    <option value="ru">{t.russian}</option>
                    <option value="en">{t.english}</option>
                  </select>
                </label>
                <label className="settingsField settingsControl settingsControlWide">
                  <span className="settingsFieldLabel">
                    <img src="/icons/nav/timezone-icon.png" alt="" />
                    {t.timezone}
                  </span>
                  <select className="select settingsSelect timezoneSelect" value={settings.timezone || ""} onChange={(event) => patch("timezone", event.target.value)}>
                    {TIMEZONE_GROUPS.map((group) => (
                      <optgroup key={group.label} label={group.label}>
                        {group.zones.map((zone) => (
                          <option key={zone.value} value={zone.value}>{timezoneLabel(zone, lang)}</option>
                        ))}
                      </optgroup>
                    ))}
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
              <div className="settingsStorageLayout">
                <label className="settingsField settingsControl settingsPathControl">
                  <span>{t.storagePath}</span>
                  <input className="input settingsInput" value={settings.storage_path || ""} onChange={(event) => patch("storage_path", event.target.value)} />
                  <span className="settingsHint">{t.hostPath}</span>
                </label>
                <div className="settingsStorageAction">
                  <button className="button secondary small" onClick={validateStorage}>{t.validate}</button>
                </div>
                {storageResult ? (
                  <div className={`settingsStatus ${storageResult.ok ? "ok" : "error"}`}>
                    <strong>{storageMessage(storageResult, lang)}</strong>
                    <span>{t.path}: {storageResult.path}</span>
                    <span>{storageResult.writable ? t.writeAllowed : t.writeDenied}</span>
                    <span>{storageResult.created ? t.created : t.exists}: {storageResult.exists ? t.yes : t.no}</span>
                    {storageResult.free_bytes != null ? <span>{t.free}: {formatBytes(storageResult.free_bytes)}</span> : null}
                  </div>
                ) : null}
              </div>
            </section>

            <section className="settingsSection settingsSectionCompact">
              <div className="settingsSectionHead">
                <h2 className="settingsSectionTitle">
                  <img src="/icons/nav/records.png" alt="" />
                  <span>{t.recording}</span>
                </h2>
                <p>{t.recordingText}</p>
              </div>
              <label className="settingsField settingsControl settingsControlWide">
                <span>{t.recordingProfile}</span>
                <select className="select settingsSelect" value={recordingProfile} onChange={(event) => setRecordingProfile(event.target.value)}>
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
              <div className="settingsHardwareLayout">
                <label className="settingsField settingsControl settingsControlWide">
                  <span>{t.hardwareMode}</span>
                  <select className="select settingsSelect" value={selectedHardware} onChange={handleHardwareChange}>
                    {hardwareSummary.map(({ backend, selectable, reason }) => (
                      <option
                        key={backend}
                        value={backend}
                        disabled={!selectable}
                        title={reason || backendLabel(backend, lang)}
                      >
                        {backendLabel(backend, lang)}{selectable ? "" : ` — ${reason || t.notDetected}`}
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
                {t.availableOptions}: {availableHardware.length ? availableHardware.map((backend) => backendLabel(backend, lang)).join(", ") : backendLabel("cpu", lang)}.
              </div>
              <div className="settingsHardwareOptions">
                {hardwareSummary.map(({ backend, selectable, reason }) => (
                  <span key={backend} className={`settingsBadge ${selectable ? "ok" : "disabled"}`} title={reason || ""}>
                    {backendLabel(backend, lang)}
                  </span>
                ))}
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

            <section className="settingsSection settingsSectionCompact">
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
