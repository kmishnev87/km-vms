"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Layout from "../../components/Layout";
import { apiFetch, apiFetchBlob, clearAuthToken } from "../../lib/api";

const UTC_TIMEZONES = Array.from({ length: 27 }, (_, index) => {
  const offset = index - 12;
  const sign = offset >= 0 ? "+" : "-";
  const label = offset === 0 ? "UTC+00:00" : `UTC${sign}${String(Math.abs(offset)).padStart(2, "0")}:00`;
  const value = offset === 0 ? "UTC" : `Etc/GMT${offset > 0 ? "-" : "+"}${Math.abs(offset)}`;
  return { offset, label, value };
});

const HARDWARE_OPTIONS = ["auto", "qsv", "vaapi", "nvenc", "amf", "cpu"];

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
    system: "Система",
    language: "Язык",
    russian: "Русский",
    english: "English",
    timezone: "Часовой пояс",
    timezoneHelp: "Часовой пояс используется для отображения времени, архива и будущей хронологии.",
    storage: "Хранилище",
    storageText: "Физический путь на NAS задаётся в docker-compose volume mapping.",
    hostPath: "Путь на сервере",
    hostPathUnknown: "Определяется в docker-compose",
    validate: "Тест",
    storageOk: "Хранилище доступно.",
    storageFail: "Хранилище недоступно",
    writeDenied: "Нет прав на запись",
    recording: "Запись",
    balanced: "Сбалансированный",
    balancedHelp: "Рекомендуемый режим. Сейчас: MKV.",
    compatibility: "Максимальная совместимость",
    compatibilityHelp: "Сейчас: MP4. Легче открыть в плеерах, но менее устойчиво при аварийном завершении.",
    reliability: "Максимальная надёжность",
    reliabilityHelp: "Сейчас: MKV. Лучше переносит прерывание процесса или сервера.",
    mapsTo: "Формат",
    hardware: "Аппаратное ускорение",
    hardwareAvailable: "Аппаратное ускорение доступно.",
    hardwareUnavailable: "Аппаратное ускорение недоступно. Будет использоваться CPU fallback.",
    selected: "Выбрано",
    unavailableMode: "Этот режим недоступен на данном сервере или не прошёл проверку.",
    rescan: "Тест",
    failedValidation: "Не прошёл проверку",
    notDetected: "Не найдено на этом сервере",
    users: "Пользователи и роли",
    usersText: "Управление пользователями, ролями и доступом к системе.",
    security: "Безопасность",
    securityText: "Журнал логирования, сбор диагностических логов и отчёт об ошибке.",
    open: "Открыть",
    close: "Закрыть",
    session: "Сессия",
    sessionText: "Сессия сохраняется до 24:00 при включённом режиме «Оставаться в системе».",
    login: "Логин",
    role: "Роль",
    status: "Статус",
    management: "Управление",
    active: "Активен",
    inactive: "Отключён",
    edit: "Изменить",
    enable: "Включить",
    disable: "Отключить",
    addUser: "Добавить пользователя",
    create: "Создать",
    creating: "Создание...",
    saveUser: "Сохранить",
    savingUser: "Сохранение...",
    displayName: "Имя",
    password: "Пароль",
    newPassword: "Новый пароль",
    adminResetPassword: "Новый пароль (сброс администратором)",
    currentPassword: "Текущий пароль",
    currentPasswordRequired: "Текущий пароль обязателен для смены собственного пароля.",
    userCreated: "Пользователь создан",
    userUpdated: "Пользователь обновлён",
    duplicateUsername: "Такой логин уже существует.",
    credentialsChanged: "Данные входа изменены. Войдите заново.",
    loggingJournal: "Журнал логирования",
    journalEmpty: "Журнал событий будет отображаться здесь после подключения backend-логирования.",
    bugReport: "Отчёт об ошибке",
    createArchive: "Создать диагностический архив",
    archiveBusy: "Создание архива...",
    archiveAttached: "Диагностический архив создан, прикреплён и скачан.",
    describeProblem: "Опишите проблему простым языком: что произошло, где нажимали, что ожидали увидеть.",
    sendReport: "Отправить отчёт",
    sendPending: "Отправка отчётов будет подключена после реализации backend-отправки.",
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
    changesApplied: "Changes applied successfully",
    storageValidated: "Storage is available. Free: {free}",
    hardwareChecked: "Hardware capabilities checked",
    system: "System",
    language: "Language",
    russian: "Русский",
    english: "English",
    timezone: "Timezone",
    timezoneHelp: "Timezone is used for displayed time, archive timestamps, and future chronology.",
    storage: "Storage",
    storageText: "The physical NAS path is defined by docker-compose volume mapping.",
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
    compatibilityHelp: "Current mapping: MP4. Easier to open in players, but less crash-safe.",
    reliability: "Maximum reliability",
    reliabilityHelp: "Current mapping: MKV. More resilient to process or server interruptions.",
    mapsTo: "Format",
    hardware: "Hardware Acceleration",
    hardwareAvailable: "Hardware acceleration is available.",
    hardwareUnavailable: "Hardware acceleration is unavailable. CPU fallback will be used.",
    selected: "Selected",
    unavailableMode: "This mode is unavailable on this server or failed validation.",
    rescan: "Test",
    failedValidation: "Failed validation",
    notDetected: "Not detected on this server",
    users: "Users and roles",
    usersText: "Manage users, roles and system access.",
    security: "Security",
    securityText: "Logging journal, diagnostic logs collection and bug report.",
    open: "Open",
    close: "Close",
    session: "Session",
    sessionText: 'Session is kept until 24:00 when "Stay signed in" is enabled.',
    login: "Login",
    role: "Role",
    status: "Status",
    management: "Management",
    active: "Active",
    inactive: "Inactive",
    edit: "Edit",
    enable: "Enable",
    disable: "Disable",
    addUser: "Add user",
    create: "Create",
    creating: "Creating...",
    saveUser: "Save",
    savingUser: "Saving...",
    displayName: "Display name",
    password: "Password",
    newPassword: "New password",
    adminResetPassword: "New password (admin reset)",
    currentPassword: "Current password",
    currentPasswordRequired: "Current password is required to change your own password.",
    userCreated: "User created",
    userUpdated: "User updated",
    duplicateUsername: "Username already exists.",
    credentialsChanged: "Login credentials changed. Please sign in again.",
    loggingJournal: "Logging journal",
    journalEmpty: "Event journal will appear here after backend logging is connected.",
    bugReport: "Bug report",
    createArchive: "Create diagnostic archive",
    archiveBusy: "Creating archive...",
    archiveAttached: "Diagnostic archive created, attached and downloaded.",
    describeProblem: "Describe the problem in simple language: what happened, where you clicked, what you expected.",
    sendReport: "Send report",
    sendPending: "Report sending will be connected after backend sending is implemented.",
    notAuthenticated: "Please sign in again.",
    forbidden: "Insufficient permissions.",
    network: "Server is unavailable.",
  },
};

const ROLE_LABELS = {
  owner: { ru: "Владелец", en: "Owner" },
  admin: { ru: "Администратор", en: "Administrator" },
  operator: { ru: "Оператор", en: "Operator" },
  viewer: { ru: "Наблюдатель", en: "Viewer" },
};

const BACKEND_LABELS = {
  auto: { ru: "Автоматический режим", en: "Automatic mode" },
  qsv: { ru: "Intel Quick Sync / QSV", en: "Intel Quick Sync / QSV" },
  vaapi: { ru: "VAAPI", en: "VAAPI" },
  nvenc: { ru: "NVIDIA NVENC/NVDEC", en: "NVIDIA NVENC/NVDEC" },
  amf: { ru: "AMD AMF", en: "AMD AMF" },
  cpu: { ru: "Резервный режим CPU", en: "CPU fallback" },
};

function languageOf(settings) {
  return settings?.language === "en" ? "en" : "ru";
}

function backendLabel(value, lang) {
  return BACKEND_LABELS[value || "auto"]?.[lang] || value || "auto";
}

function roleLabel(role, lang) {
  return ROLE_LABELS[role]?.[lang] || role;
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
  const raw = typeof detail?.detail === "string" ? detail.detail : message;

  if (storage?.error) return `${t.storageFail}: ${storage.error}`;
  if (raw.includes("Username already exists")) return t.duplicateUsername;
  if (raw.includes("Current password is required")) return t.currentPasswordRequired;
  if (raw.includes("401") || raw.includes("Not authenticated")) return t.notAuthenticated;
  if (raw.includes("403") || raw.includes("Forbidden") || raw.includes("Insufficient permissions")) return t.forbidden;
  if (raw.includes("Failed to fetch") || raw.includes("NetworkError")) return t.network;
  return raw || t.network;
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

const blankUserForm = {
  username: "",
  full_name: "",
  password: "",
  current_password: "",
  role: "viewer",
  is_active: true,
};

export default function SettingsPage() {
  const router = useRouter();
  const [settings, setSettings] = useState(null);
  const [hardware, setHardware] = useState(null);
  const [recordingProfile, setRecordingProfile] = useState("balanced");
  const [toast, setToast] = useState(null);
  const [busy, setBusy] = useState(false);
  const [usersOpen, setUsersOpen] = useState(false);
  const [securityOpen, setSecurityOpen] = useState(false);
  const [users, setUsers] = useState([]);
  const [currentUser, setCurrentUser] = useState(null);
  const [usersLoading, setUsersLoading] = useState(false);
  const [userSaving, setUserSaving] = useState(false);
  const [userError, setUserError] = useState("");
  const [userMode, setUserMode] = useState(null);
  const [userForm, setUserForm] = useState(blankUserForm);
  const [editingUser, setEditingUser] = useState(null);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [attachedArchive, setAttachedArchive] = useState(null);
  const [reportText, setReportText] = useState("");
  const toastTimerRef = useRef(null);
  const lang = languageOf(settings);
  const t = TEXT[lang] || TEXT.ru;
  const languageIcon = lang === "en" ? "/icons/nav/language-icon_ENG.png" : "/icons/nav/language-icon_RU.png";

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
    toastTimerRef.current = window.setTimeout(() => setToast(null), 3200);
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

  async function openUsersModal() {
    setUsersOpen(true);
    await loadUsers();
  }

  async function loadUsers() {
    setUsersLoading(true);
    try {
      const [me, list] = await Promise.all([apiFetch("/auth/me"), apiFetch("/users")]);
      setCurrentUser(me);
      setUsers(list);
    } catch (err) {
      showToast(humanError(err, lang), "error");
    } finally {
      setUsersLoading(false);
    }
  }

  function assignableRoles(target = null) {
    if (currentUser?.role === "owner") {
      if (target?.id === currentUser.id) return ["owner"];
      return ["admin", "operator", "viewer"];
    }
    if (currentUser?.role === "admin") return ["operator", "viewer"];
    return [];
  }

  function canEditUser(user) {
    if (!currentUser) return false;
    if (currentUser.role === "owner") return true;
    return currentUser.role === "admin" && user.role !== "owner";
  }

  function canToggleUser(user) {
    if (!canEditUser(user)) return false;
    if (user.id === currentUser?.id) return false;
    if (user.role === "owner") return false;
    return true;
  }

  function startCreateUser() {
    const roles = assignableRoles();
    setUserMode("create");
    setEditingUser(null);
    setUserError("");
    setUserForm({ ...blankUserForm, role: roles[0] || "viewer" });
  }

  function startEditUser(user) {
    setUserMode("edit");
    setEditingUser(user);
    setUserError("");
    setUserForm({
      username: user.username,
      full_name: user.full_name || "",
      password: "",
      current_password: "",
      role: user.role,
      is_active: user.is_active,
    });
  }

  async function submitUserForm(event) {
    event.preventDefault();
    setUserSaving(true);
    setUserError("");
    try {
      if (userMode === "create") {
        await apiFetch("/users", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username: userForm.username,
            full_name: userForm.full_name,
            password: userForm.password,
            role: userForm.role,
            is_active: userForm.is_active,
          }),
        });
        setUserMode(null);
        await loadUsers();
        showToast(t.userCreated);
      } else if (editingUser) {
        const payload = {
          username: userForm.username,
          full_name: userForm.full_name,
          is_active: userForm.is_active,
        };
        if (userForm.role !== editingUser.role) payload.role = userForm.role;
        if (userForm.password) {
          payload.password = userForm.password;
          if (editingUser.id === currentUser?.id) payload.current_password = userForm.current_password;
        }
        const result = await apiFetch(`/users/${editingUser.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (result?.own_credentials_changed) {
          showToast(t.credentialsChanged);
          clearAuthToken();
          window.setTimeout(() => router.replace("/login"), 500);
          return;
        }
        setUserMode(null);
        await loadUsers();
        showToast(t.userUpdated);
      }
    } catch (err) {
      const message = humanError(err, lang);
      setUserError(message);
      showToast(message, "error");
    } finally {
      setUserSaving(false);
    }
  }

  async function toggleUser(user) {
    try {
      await apiFetch(`/users/${user.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: !user.is_active }),
      });
      await loadUsers();
      showToast(t.userUpdated);
    } catch (err) {
      showToast(humanError(err, lang), "error");
    }
  }

  async function createDiagnosticArchive() {
    setArchiveBusy(true);
    setAttachedArchive(null);
    try {
      const { blob, filename } = await apiFetchBlob("/diagnostics/archive", { method: "POST" });
      const name = filename || "km-vms-diagnostics.zip";
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setAttachedArchive({ filename: name, size: blob.size });
      showToast(t.archiveAttached);
    } catch (err) {
      showToast(humanError(err, lang), "error");
    } finally {
      setArchiveBusy(false);
    }
  }

  function sendReport() {
    showToast(t.sendPending, "error");
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

  const userFormRoles = assignableRoles(editingUser);
  const currentUserSummary = currentUser
    ? `${currentUser.username} / ${currentUser.full_name || "-"} / ${roleLabel(currentUser.role, lang)}`
    : "-";

  return (
    <Layout>
      <div className="settingsPage">
        {toast ? (
          <div className={`settingsToast ${toast.type}`}>
            <strong>{toast.title}</strong>
            {toast.subtitle ? <span>{toast.subtitle}</span> : null}
          </div>
        ) : null}

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
                  <span>{t.system}</span>
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
                  <strong>{t.timezone}</strong>
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
                  <strong>{t.storage}</strong>
                  <span>{t.storageText}</span>
                  <small>{t.hostPath}: {settings.storage_host_path || t.hostPathUnknown}</small>
                </div>
                <div className="settingsRowControl stacked">
                  <input className="input settingsInput" value={settings.storage_path || ""} onChange={(event) => patch("storage_path", event.target.value)} />
                </div>
                <div className="settingsRowAction">
                  <button className="button secondary small" onClick={validateStorage}>{t.validate}</button>
                </div>
              </div>

              <div className="settingsRow">
                <div className="settingsRowIcon"><img src="/icons/nav/records.png" alt="" /></div>
                <div className="settingsRowText">
                  <strong>{t.recording}</strong>
                  <span>{profileHelp} {t.mapsTo}: {recordingFormatForProfile(recordingProfile).toUpperCase()}.</span>
                </div>
                <div className="settingsRowControl">
                  <select className="select settingsSelect" value={recordingProfile} onChange={(event) => setRecordingProfile(event.target.value)}>
                    <option value="balanced">{t.balanced}</option>
                    <option value="compatibility">{t.compatibility}</option>
                    <option value="reliability">{t.reliability}</option>
                  </select>
                </div>
              </div>

              <div className="settingsRow">
                <div className="settingsRowIcon"><img src="/icons/nav/hardware-icon.png" alt="" /></div>
                <div className="settingsRowText">
                  <strong>{t.hardware}</strong>
                  <span>{hardware?.hardware_accel_available ? t.hardwareAvailable : t.hardwareUnavailable} {t.selected}: {backendLabel(selectedHardware, lang)}.</span>
                  <div className="settingsHardwareOptions">
                    {hardwareSummary.map(({ backend, selectable, reason }) => (
                      <span key={backend} className={`settingsBadge ${selectable ? "ok" : "disabled"}`} title={reason || ""}>
                        {backendLabel(backend, lang)}
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
                </div>
                <div className="settingsRowAction">
                  <button className="button secondary small" onClick={rescanHardware}>{t.rescan}</button>
                </div>
              </div>

              <div className="settingsRow">
                <div className="settingsRowIcon"><img src="/icons/nav/users-icon.png" alt="" /></div>
                <div className="settingsRowText">
                  <strong>{t.users}</strong>
                  <span>{t.usersText}</span>
                </div>
                <div className="settingsRowControl" />
                <div className="settingsRowAction">
                  <button className="button secondary small" onClick={openUsersModal}>{t.open}</button>
                </div>
              </div>

              <div className="settingsRow">
                <div className="settingsRowIcon"><img src="/icons/nav/security-icon.png" alt="" /></div>
                <div className="settingsRowText">
                  <strong>{t.security}</strong>
                  <span>{t.securityText}</span>
                </div>
                <div className="settingsRowControl" />
                <div className="settingsRowAction">
                  <button className="button secondary small" onClick={() => setSecurityOpen(true)}>{t.open}</button>
                </div>
              </div>
            </section>
          </div>
        )}

        {usersOpen ? (
          <div className="modalBackdrop">
            <div className="modal modalWide settingsModal">
              <div className="modalHeader">
                <h2>{t.users}</h2>
                <button className="iconCloseButton" onClick={() => setUsersOpen(false)} aria-label={t.close}>×</button>
              </div>
              <div className="settingsModalGrid">
                <div className="settingsInfoTile">
                  <strong>{currentUser?.username || "-"}</strong>
                  <span>{currentUserSummary}</span>
                </div>
                <div className="settingsInfoTile">
                  <strong>{t.session}</strong>
                  <span>24:00</span>
                  <small>{t.sessionText}</small>
                </div>
              </div>
              <div className="settingsModalToolbar">
                <button className="button small" onClick={startCreateUser} disabled={!assignableRoles().length}>{t.addUser}</button>
              </div>
              {usersLoading ? <div className="settingsEmptyState">{t.saving}</div> : (
                <div className="settingsTableWrap">
                  <table className="table settingsUsersTable">
                    <thead>
                      <tr>
                        <th>{t.login}</th>
                        <th>{t.role}</th>
                        <th>{t.status}</th>
                        <th>{t.management}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {users.map((user) => (
                        <tr key={user.id}>
                          <td><strong>{user.username}</strong><small>{user.full_name || "-"}</small></td>
                          <td>{roleLabel(user.role, lang)}</td>
                          <td><span className={`badge ${user.is_active ? "ok" : "err"}`}>{user.is_active ? t.active : t.inactive}</span></td>
                          <td>
                            <div className="settingsUserActions">
                              <button className="button secondary small" onClick={() => startEditUser(user)} disabled={!canEditUser(user)}>{t.edit}</button>
                              <button className="button secondary small" onClick={() => toggleUser(user)} disabled={!canToggleUser(user)}>
                                {user.is_active ? t.disable : t.enable}
                              </button>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        ) : null}

        {userMode ? (
          <div className="modalBackdrop">
            <form className="modal settingsEditModal" onSubmit={submitUserForm}>
              <div className="modalHeader">
                <h2>{userMode === "create" ? t.addUser : t.edit}</h2>
                <button type="button" className="iconCloseButton" onClick={() => setUserMode(null)} aria-label={t.close}>×</button>
              </div>
              <div className="formGrid">
                <label>
                  <div className="formLabel">{t.login}</div>
                  <input className="input" value={userForm.username} onChange={(event) => setUserForm({ ...userForm, username: event.target.value })} required />
                </label>
                <label>
                  <div className="formLabel">{t.displayName}</div>
                  <input className="input" value={userForm.full_name} onChange={(event) => setUserForm({ ...userForm, full_name: event.target.value })} />
                </label>
                <label>
                  <div className="formLabel">{t.role}</div>
                  <select className="select" value={userForm.role} onChange={(event) => setUserForm({ ...userForm, role: event.target.value })} disabled={editingUser?.id === currentUser?.id}>
                    {(userFormRoles.includes(userForm.role) ? userFormRoles : [userForm.role]).map((role) => (
                      <option key={role} value={role}>{roleLabel(role, lang)}</option>
                    ))}
                  </select>
                </label>
                <label>
                  <div className="formLabel">{t.status}</div>
                  <select className="select" value={userForm.is_active ? "1" : "0"} onChange={(event) => setUserForm({ ...userForm, is_active: event.target.value === "1" })} disabled={editingUser?.id === currentUser?.id || editingUser?.role === "owner"}>
                    <option value="1">{t.active}</option>
                    <option value="0">{t.inactive}</option>
                  </select>
                </label>
                {editingUser?.id === currentUser?.id ? (
                  <label>
                    <div className="formLabel">{t.currentPassword}</div>
                    <input className="input" type="password" value={userForm.current_password} onChange={(event) => setUserForm({ ...userForm, current_password: event.target.value })} autoComplete="current-password" />
                  </label>
                ) : null}
                <label>
                  <div className="formLabel">{userMode === "create" ? t.password : editingUser?.id === currentUser?.id ? t.newPassword : t.adminResetPassword}</div>
                  <input className="input" type="password" value={userForm.password} onChange={(event) => setUserForm({ ...userForm, password: event.target.value })} required={userMode === "create"} autoComplete="new-password" />
                </label>
              </div>
              {userError ? <div className="settingsFormError">{userError}</div> : null}
              <div className="actions">
                <button type="button" className="button secondary" onClick={() => setUserMode(null)}>{t.close}</button>
                <button className="button" disabled={userSaving}>{userSaving ? (userMode === "create" ? t.creating : t.savingUser) : (userMode === "create" ? t.create : t.saveUser)}</button>
              </div>
            </form>
          </div>
        ) : null}

        {securityOpen ? (
          <div className="modalBackdrop">
            <div className="modal modalWide settingsModal">
              <div className="modalHeader">
                <h2>{t.security}</h2>
                <button className="iconCloseButton" onClick={() => { setSecurityOpen(false); setAttachedArchive(null); setReportText(""); }} aria-label={t.close}>×</button>
              </div>
              <section className="settingsSecuritySection">
                <h3>{t.loggingJournal}</h3>
                <div className="settingsJournalArea">{t.journalEmpty}</div>
              </section>
              <section className="settingsSecuritySection">
                <h3>{t.bugReport}</h3>
                <button className="button secondary" onClick={createDiagnosticArchive} disabled={archiveBusy}>{archiveBusy ? t.archiveBusy : t.createArchive}</button>
                {attachedArchive ? (
                  <div className="settingsAttachedArchive">
                    <strong>{t.archiveAttached}</strong>
                    <span>{attachedArchive.filename}</span>
                  </div>
                ) : null}
                <textarea className="input settingsReportTextarea" value={reportText} onChange={(event) => setReportText(event.target.value)} placeholder={t.describeProblem} />
                <button className="button" onClick={sendReport} disabled={!attachedArchive || !reportText.trim()}>{t.sendReport}</button>
              </section>
            </div>
          </div>
        ) : null}
      </div>
    </Layout>
  );
}
