"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

const COPY = {
  ru: {
    title: "Первый запуск KM VMS",
    subtitle: "Пошагово создайте владельца системы и подтвердите базовые параметры.",
    steps: ["Язык", "Владелец", "Хранилище", "Запись", "Проверка"],
    welcomeTitle: "Добро пожаловать",
    welcomeText: "Этот мастер выполняется один раз до входа в систему.",
    systemName: "Имя системы",
    systemNameHelp: "Несекретное имя продукта, которое отображается в настройках.",
    language: "Язык интерфейса",
    ownerTitle: "Владелец системы",
    username: "Логин владельца",
    password: "Пароль",
    passwordConfirm: "Повтор пароля",
    usernameHelp: "2-64 символа: латиница, цифры, точка, дефис или подчеркивание.",
    storageTitle: "Хранилище архива",
    storageHelp: "Выберите NAS/server папку для архива. Внутри Docker путь остается /storage/archive.",
    storageFolder: "Папка архива",
    storagePreview: "NAS/server путь",
    storageTechnical: "Docker путь",
    storageUnavailable: "Выбор диска недоступен: нужен host snapshot от installer.",
    storageApply: "Проверить и выбрать",
    storageAllowed: "доступен",
    storageBlocked: "заблокирован",
    storageWritable: "запись",
    storageReadOnly: "только чтение",
    storageTotal: "всего",
    storageUsed: "занято",
    storageFree: "свободно",
    storageReason: "причина",
    storagePending: "Выбор записан как pending: host helper/restart должен применить mount.",
    storageBlockedReady: "Сначала выберите и подтвердите NAS/server папку архива.",
    nextAction: "Следующее действие",
    finalLockNote: "После завершения первый запуск будет закрыт.",
    recordingTitle: "Параметры записи",
    timezone: "Часовой пояс",
    format: "Формат записи",
    formatHelp: "MKV = надежность, MP4 = совместимость.",
    reviewTitle: "Проверка перед завершением",
    reviewNote: "Имя системы будет сохранено как несекретная настройка продукта.",
    back: "Назад",
    next: "Далее",
    submit: "Завершить настройку",
    busy: "Сохраняем...",
    mismatch: "Пароли не совпадают.",
    required: "Заполните обязательные поля.",
    invalidUsername: "Логин содержит недопустимые символы.",
  },
  en: {
    title: "KM VMS first run",
    subtitle: "Create the system owner and confirm baseline settings step by step.",
    steps: ["Language", "Owner", "Storage", "Recording", "Review"],
    welcomeTitle: "Welcome",
    welcomeText: "This wizard runs once before the first sign-in.",
    systemName: "System name",
    systemNameHelp: "Non-secret product name shown in settings.",
    language: "Interface language",
    ownerTitle: "System owner",
    username: "Owner login",
    password: "Password",
    passwordConfirm: "Confirm password",
    usernameHelp: "2-64 characters: letters, numbers, dot, dash or underscore.",
    storageTitle: "Archive storage",
    storageHelp: "Choose the NAS/server archive folder. The Docker path remains /storage/archive.",
    storageFolder: "Archive folder",
    storagePreview: "NAS/server path",
    storageTechnical: "Docker path",
    storageUnavailable: "Disk selection is unavailable: installer host snapshot is required.",
    storageApply: "Validate and select",
    storageAllowed: "allowed",
    storageBlocked: "blocked",
    storageWritable: "writable",
    storageReadOnly: "read-only",
    storageTotal: "total",
    storageUsed: "used",
    storageFree: "free",
    storageReason: "reason",
    storagePending: "Selection is pending: host helper/restart must apply the mount.",
    storageBlockedReady: "Choose and confirm the NAS/server archive folder first.",
    nextAction: "Next action",
    finalLockNote: "First-run mode will be locked after finish.",
    recordingTitle: "Recording defaults",
    timezone: "Timezone",
    format: "Recording format",
    formatHelp: "MKV = reliability, MP4 = compatibility.",
    reviewTitle: "Review before finish",
    reviewNote: "System name will be saved as a non-secret product setting.",
    back: "Back",
    next: "Next",
    submit: "Finish setup",
    busy: "Saving...",
    mismatch: "Passwords do not match.",
    required: "Fill in the required fields.",
    invalidUsername: "Username contains unsupported characters.",
  },
};

const USERNAME_RE = /^[A-Za-z0-9_.-]{2,64}$/;

export default function SetupPage() {
  const router = useRouter();
  const [step, setStep] = useState(0);
  const [language, setLanguage] = useState("ru");
  const [form, setForm] = useState({
    username: "admin",
    system_name: "KM VMS",
    password: "",
    password_confirm: "",
    timezone: "Asia/Yekaterinburg",
    storage_path: "",
    recording_format: "mkv",
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [storageState, setStorageState] = useState({
    loading: true,
    candidates: [],
    candidateId: "",
    folderName: "KM-VMS-Recordings",
    preview: null,
    confirmation: null,
    message: "",
    error: "",
  });

  const t = COPY[language];
  const ownerValid = USERNAME_RE.test(form.username.trim()) && form.password.length >= 8 && form.password === form.password_confirm;
  const systemNameValid = form.system_name.trim().length <= 80 && !/[\x00-\x1f]/.test(form.system_name);
  const storageReady = Boolean(storageState.confirmation?.ready && storageState.confirmation?.selected_host_path);
  const recordingValid = Boolean(form.timezone.trim()) && ["mkv", "mp4"].includes(form.recording_format);
  const canAdvance = [systemNameValid, ownerValid, storageReady, recordingValid, systemNameValid && ownerValid && storageReady && recordingValid][step];

  const selectedCandidate = useMemo(
    () => storageState.candidates.find((candidate) => candidate.id === storageState.candidateId),
    [storageState.candidates, storageState.candidateId],
  );

  function patch(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function storageConfirmationFromApply(data) {
    return data?.storage_confirmation || {
      ready: Boolean(data?.final_host_path),
      selected_host_path: data?.final_host_path || null,
      container_archive_path: data?.container_archive_path || "/storage/archive",
      status: data?.apply_status || "unavailable",
      apply_status: data?.apply_status || null,
      restart_required: data?.apply_status !== "active",
      manual_action_required: data?.apply_status !== "active",
      next_action: data?.apply_status === "pending_host_helper_restart_required" ? "run_storage_apply_helper_and_restart" : "select_and_validate_storage",
    };
  }

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (!bytes) return "-";
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let size = bytes;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return `${size >= 10 || unit === 0 ? Math.round(size) : size.toFixed(1)} ${units[unit]}`;
  }

  function candidateText(candidate) {
    const total = Number(candidate.total_bytes || 0);
    const free = Number(candidate.free_bytes || 0);
    const freePercent = total ? Math.round((free / total) * 100) : null;
    return [
      `${t.storageTotal}: ${formatBytes(candidate.total_bytes)}`,
      `${t.storageUsed}: ${formatBytes(candidate.used_bytes)}`,
      `${t.storageFree}: ${formatBytes(candidate.free_bytes)}${freePercent === null ? "" : ` (${freePercent}%)`}`,
      candidate.writable ? t.storageWritable : t.storageReadOnly,
      candidate.safety_status === "allowed" ? t.storageAllowed : t.storageBlocked,
      candidate.reason ? `${t.storageReason}: ${candidate.reason}` : "",
    ].filter(Boolean).join(" | ");
  }

  useEffect(() => {
    fetch("/api/system/status")
      .then((response) => response.ok ? response.json() : null)
      .then((status) => {
        if (status?.initialized) router.replace("/login");
      })
      .catch(() => {});
  }, [router]);

  useEffect(() => {
    fetch("/api/setup/storage/discovery")
      .then((response) => response.ok ? response.json() : null)
      .then((data) => {
        const candidates = data?.candidates || [];
        const allowed = candidates.filter((item) => item.safety_status === "allowed");
        setStorageState((current) => ({
          ...current,
          loading: false,
          candidates,
          candidateId: allowed[0]?.id || "",
          error: allowed.length ? "" : t.storageUnavailable,
        }));
      })
      .catch(() => setStorageState((current) => ({ ...current, loading: false, error: t.storageUnavailable })));
  }, [t.storageUnavailable]);

  useEffect(() => {
    fetch("/api/setup/storage/status")
      .then((response) => response.ok ? response.json() : null)
      .then((data) => {
        if (!data?.ready) return;
        patch("storage_path", data.selected_host_path || "");
        setStorageState((current) => ({ ...current, confirmation: data, preview: data, message: data.status || "" }));
      })
      .catch(() => {});
  }, []);

  function validateCurrentStep() {
    if (step === 0 && !systemNameValid) return t.required;
    if (step === 1) {
      if (!USERNAME_RE.test(form.username.trim())) return t.invalidUsername;
      if (!form.password || !form.password_confirm) return t.required;
      if (form.password !== form.password_confirm) return t.mismatch;
    }
    if (step === 3 && !recordingValid) return t.required;
    if (step === 2 && !storageReady) return t.storageBlockedReady;
    return "";
  }

  function canVisitStep(index) {
    if (index <= step) return true;
    if (index === 1) return systemNameValid;
    if (index === 2) return ownerValid;
    if (index === 3) return ownerValid && storageReady;
    return systemNameValid && ownerValid && storageReady && recordingValid;
  }

  function goToStep(index) {
    if (index <= step) {
      setError("");
      setStep(index);
      return;
    }
    const message = validateCurrentStep();
    if (message) {
      setError(message);
      return;
    }
    if (!canVisitStep(index)) {
      setError(t.required);
      return;
    }
    setError("");
    setStep(index);
  }

  function nextStep() {
    goToStep(Math.min(step + 1, t.steps.length - 1));
  }

  async function selectStorage() {
    setStorageState((current) => ({ ...current, error: "", message: "" }));
    try {
      const payload = { candidate_id: storageState.candidateId, folder_name: storageState.folderName };
      const previewResponse = await fetch("/api/setup/storage/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const previewData = await previewResponse.json().catch(() => null);
      if (!previewResponse.ok) throw new Error(previewData?.detail || "Storage preview failed");
      const applyResponse = await fetch("/api/setup/storage/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const applyData = await applyResponse.json().catch(() => null);
      if (!applyResponse.ok) throw new Error(applyData?.detail || "Storage validation failed");
      const confirmation = storageConfirmationFromApply(applyData);
      patch("storage_path", confirmation.selected_host_path || "");
      setStorageState((current) => ({
        ...current,
        preview: applyData,
        confirmation,
        message: applyData.apply_status === "pending_host_helper_restart_required" ? t.storagePending : (applyData.apply_status || "selected"),
      }));
    } catch (err) {
      setStorageState((current) => ({ ...current, error: err?.message || "Storage selection failed" }));
    }
  }

  async function submit(e) {
    e.preventDefault();
    if (!systemNameValid || !ownerValid || !storageReady || !recordingValid) {
      setError(t.required);
      return;
    }
    setError("");
    setBusy(true);
    try {
      const response = await fetch("/api/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...form, username: form.username.trim(), system_name: form.system_name.trim() || null, storage_path: storageState.confirmation?.selected_host_path || "", language }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(typeof data?.detail === "string" ? data.detail : data?.detail?.error || data?.detail?.storage?.error || "Setup failed");
      }
      router.replace("/login");
    } catch (err) {
      setError(err?.message || "Setup failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="setupPage">
      <form className="setupCard setupWizard" onSubmit={submit}>
        <div className="setupHeader">
          <div>
            <h1>{t.title}</h1>
            <p>{t.subtitle}</p>
          </div>
          <select className="select setupLang" value={language} onChange={(e) => setLanguage(e.target.value)}>
            <option value="ru">RU</option>
            <option value="en">EN</option>
          </select>
        </div>

        <div className="setupSteps" aria-label="Setup progress">
          {t.steps.map((label, index) => (
            <button className={`setupStep ${index === step ? "active" : ""} ${index < step ? "done" : ""}`} type="button" key={label} onClick={() => goToStep(index)} disabled={busy || !canVisitStep(index)}>
              <span>{index + 1}</span>
              <strong>{label}</strong>
            </button>
          ))}
        </div>

        <div className="setupWizardBody">
          {step === 0 ? (
            <section className="setupPane">
              <h2>{t.welcomeTitle}</h2>
              <p>{t.welcomeText}</p>
              <label className="settingsField">
                <span>{t.systemName}</span>
                <input className="input" value={form.system_name} onChange={(e) => patch("system_name", e.target.value)} maxLength={80} />
                <small>{t.systemNameHelp}</small>
              </label>
              <label className="settingsField">
                <span>{t.language}</span>
                <select className="select" value={language} onChange={(e) => setLanguage(e.target.value)}>
                  <option value="ru">Русский</option>
                  <option value="en">English</option>
                </select>
              </label>
            </section>
          ) : null}

          {step === 1 ? (
            <section className="setupPane">
              <h2>{t.ownerTitle}</h2>
              <div className="settingsGrid">
                <label className="settingsField">
                  <span>{t.username}</span>
                  <input className="input" value={form.username} onChange={(e) => patch("username", e.target.value)} autoComplete="username" />
                  <small>{t.usernameHelp}</small>
                </label>
                <label className="settingsField">
                  <span>{t.password}</span>
                  <input className="input" type="password" value={form.password} onChange={(e) => patch("password", e.target.value)} autoComplete="new-password" />
                </label>
                <label className="settingsField">
                  <span>{t.passwordConfirm}</span>
                  <input className="input" type="password" value={form.password_confirm} onChange={(e) => patch("password_confirm", e.target.value)} autoComplete="new-password" />
                </label>
              </div>
            </section>
          ) : null}

          {step === 2 ? (
            <section className="setupPane">
              <h2>{t.storageTitle}</h2>
              <p>{t.storageHelp}</p>
              <div className="settingsGrid">
                <label className="settingsField settingsFull">
                  <span>{t.storageTitle}</span>
                  <select
                    className="select"
                    value={storageState.candidateId}
                    onChange={(e) => setStorageState((current) => ({ ...current, candidateId: e.target.value, preview: null, confirmation: null }))}
                    disabled={storageState.loading || !storageState.candidates.some((candidate) => candidate.safety_status === "allowed")}
                  >
                    {storageState.candidates.map((candidate) => (
                      <option value={candidate.id} key={candidate.id} disabled={candidate.safety_status !== "allowed"}>
                        {candidate.label} - {candidate.safety_status === "allowed" ? t.storageAllowed : t.storageBlocked} - {t.storageFree}: {formatBytes(candidate.free_bytes)}
                      </option>
                    ))}
                  </select>
                  {selectedCandidate ? <small>{selectedCandidate.path}: {candidateText(selectedCandidate)}</small> : null}
                </label>
                <label className="settingsField">
                  <span>{t.storageFolder}</span>
                  <input className="input" value={storageState.folderName} onChange={(e) => setStorageState((current) => ({ ...current, folderName: e.target.value, preview: null, confirmation: null }))} disabled={!storageState.candidates.length} />
                </label>
                <div className="settingsField setupActionField">
                  <span>&nbsp;</span>
                  <button className="button secondary small" type="button" onClick={selectStorage} disabled={!storageState.candidateId || busy}>
                    {t.storageApply}
                  </button>
                </div>
                <div className="settingsStatus settingsFull compact">
                  <strong>{t.storagePreview}</strong>
                  <span>{storageState.confirmation?.selected_host_path || t.storageBlockedReady}</span>
                  <strong>{t.storageTechnical}</strong>
                  <span>{storageState.confirmation?.container_archive_path || "/storage/archive"}</span>
                  <strong>Status</strong>
                  <span>{storageState.confirmation?.status || "unavailable"}</span>
                  {storageState.confirmation?.next_action ? (
                    <>
                      <strong>{t.nextAction}</strong>
                      <span>{storageState.confirmation.next_action}</span>
                    </>
                  ) : null}
                  {storageState.message ? <span>{storageState.message}</span> : null}
                  {storageState.error ? <span>{storageState.error}</span> : null}
                </div>
              </div>
            </section>
          ) : null}

          {step === 3 ? (
            <section className="setupPane">
              <h2>{t.recordingTitle}</h2>
              <div className="settingsGrid">
                <label className="settingsField">
                  <span>{t.timezone}</span>
                  <input className="input" value={form.timezone} onChange={(e) => patch("timezone", e.target.value)} />
                </label>
                <label className="settingsField">
                  <span>{t.format}</span>
                  <select className="select" value={form.recording_format} onChange={(e) => patch("recording_format", e.target.value)}>
                    <option value="mkv">MKV</option>
                    <option value="mp4">MP4</option>
                  </select>
                  <small>{t.formatHelp}</small>
                </label>
              </div>
            </section>
          ) : null}

          {step === 4 ? (
            <section className="setupPane">
              <h2>{t.reviewTitle}</h2>
              <div className="setupReviewGrid">
                <span>{t.systemName}</span><strong>{form.system_name.trim() || "KM VMS"}</strong>
                <span>{t.language}</span><strong>{language.toUpperCase()}</strong>
                <span>{t.username}</span><strong>{form.username.trim()}</strong>
                <span>{t.storagePreview}</span><strong>{storageState.confirmation?.selected_host_path || t.storageBlockedReady}</strong>
                <span>{t.storageTechnical}</span><strong>{storageState.confirmation?.container_archive_path || "/storage/archive"}</strong>
                <span>Status</span><strong>{storageState.confirmation?.status || "unavailable"}</strong>
                <span>{t.nextAction}</span><strong>{storageState.confirmation?.next_action || "-"}</strong>
                <span>{t.timezone}</span><strong>{form.timezone}</strong>
                <span>{t.format}</span><strong>{form.recording_format.toUpperCase()}</strong>
              </div>
              <p>{t.reviewNote}</p>
              <p>{t.finalLockNote}</p>
            </section>
          ) : null}
        </div>

        {error ? <div className="authError">{error}</div> : null}
        <div className="setupActions">
          <button className="button secondary" type="button" onClick={() => setStep((current) => Math.max(current - 1, 0))} disabled={busy || step === 0}>
            {t.back}
          </button>
          {step < t.steps.length - 1 ? (
            <button className="button" type="button" onClick={nextStep} disabled={busy || !canAdvance}>
              {t.next}
            </button>
          ) : (
            <button className="button" type="submit" disabled={busy || !canAdvance}>
              {busy ? t.busy : t.submit}
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
