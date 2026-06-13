"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { LanguageSelect, localeMetadata, normalizeLocale, useI18n, useLocaleText } from "../../lib/i18n";

const USERNAME_RE = /^[A-Za-z0-9_.-]{2,64}$/;
const ACTIVATION_STATUSES = new Set(["activation_requested", "activation_in_progress", "applied_restart_required"]);

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

export default function SetupPage() {
  const router = useRouter();
  const { setLocale } = useI18n();
  const t = useLocaleText("setup");
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
    manualPathSupported: false,
    manualRootPath: "",
    preview: null,
    previewError: "",
    confirmation: null,
    message: "",
    error: "",
  });

  const ownerValid = USERNAME_RE.test(form.username.trim()) && form.password.length >= 8 && form.password === form.password_confirm;
  const systemNameValid = form.system_name.trim().length <= 80 && !/[\x00-\x1f]/.test(form.system_name);
  const recordingValid = Boolean(form.timezone.trim()) && ["mkv", "mp4"].includes(form.recording_format);

  const usingManualRoot = storageState.candidateId === "manual";
  const selectedCandidate = useMemo(
    () => storageState.candidates.find((candidate) => candidate.id === storageState.candidateId),
    [storageState.candidates, storageState.candidateId],
  );
  const selectedRootPath = usingManualRoot ? storageState.manualRootPath.trim() : (selectedCandidate?.path || "");
  const storageStatus = storageState.confirmation?.status || "";
  const storageReady = Boolean(storageState.confirmation?.ready && storageState.confirmation?.selected_host_path);
  const activationInProgress = ACTIVATION_STATUSES.has(storageState.confirmation?.apply_status || storageStatus);
  const actionLabel = storageState.preview?.action === "create_and_select" ? t.storageCreateSelect : t.storageCheckSelect;
  const canAdvance = [systemNameValid, ownerValid, storageReady, recordingValid, systemNameValid && ownerValid && storageReady && recordingValid][step];

  function patch(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function changeLanguage(nextLanguage) {
    const normalized = normalizeLocale(nextLanguage);
    setLanguage(normalized);
    setLocale(normalized);
  }

  function storageStatusText(status) {
    if (status === "active") return t.storageStatusActive;
    if (status === "activation_requested") return t.storageStatusQueued;
    if (status === "activation_in_progress") return t.storageStatusActivating;
    if (status === "applied_restart_required") return t.storageStatusRestarting;
    if (status === "activation_failed" || status === "validation_failed") return t.storageStatusFailed;
    return t.storageStatusUnavailable;
  }

  function nextActionText(nextAction) {
    if (nextAction === "continue_setup") return t.storageNextContinue;
    if (nextAction === "wait_for_storage_activation") return t.storageNextWait;
    if (nextAction === "resolve_storage_activation_error") return t.storageNextFix;
    return t.storageNextSelect;
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
      candidate.recommended ? t.storageRecommended : "",
    ].filter(Boolean).join(" | ");
  }

  function storageDisabledReason() {
    if (storageState.loading) return t.storageLoading;
    if (!storageState.candidates.length && !storageState.manualPathSupported) return t.storageUnavailable;
    if (!selectedRootPath) return usingManualRoot ? t.storageManualRootRequired : t.storageRootRequired;
    if (!storageState.folderName.trim()) return t.storageFolderRequired;
    if (storageState.previewError) return storageState.previewError;
    if (activationInProgress) return t.storageActivationBusy;
    if (!storageState.preview) return t.storagePreviewPending;
    if (storageState.preview.blockers?.length) return t.storageBlockedByPreview;
    return "";
  }

  useEffect(() => {
    fetch("/api/system/status")
      .then((response) => (response.ok ? response.json() : null))
      .then((status) => {
        if (status?.initialized) router.replace("/login");
      })
      .catch(() => {});
  }, [router]);

  useEffect(() => {
    fetch("/api/setup/storage/discovery")
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        const candidates = data?.candidates || [];
        const nextCandidateId = candidates.find((item) => item.recommended)?.id || candidates[0]?.id || (data?.manual_path_supported ? "manual" : "");
        setStorageState((current) => ({
          ...current,
          loading: false,
          candidates,
          manualPathSupported: Boolean(data?.manual_path_supported),
          candidateId: current.candidateId || nextCandidateId,
          error: candidates.length || data?.manual_path_supported ? "" : t.storageUnavailable,
        }));
      })
      .catch(() => setStorageState((current) => ({ ...current, loading: false, error: t.storageUnavailable })));
  }, [t.storageUnavailable]);

  useEffect(() => {
    let cancelled = false;
    async function loadStatus() {
      try {
        const response = await fetch("/api/setup/storage/status");
        const data = response.ok ? await response.json() : null;
        if (!data || cancelled) return;
        patch("storage_path", data.selected_host_path || "");
        setStorageState((current) => ({
          ...current,
          confirmation: data,
          message: data.apply_status ? storageStatusText(data.apply_status) : "",
          error: data.apply_status === "activation_failed" ? (data.apply_state?.error || t.storageActivationFailed) : current.error,
        }));
      } catch {
        if (!cancelled) {
          setStorageState((current) => ({ ...current, error: current.error || t.storageStatusUnavailable }));
        }
      }
    }
    loadStatus();
    let timer = null;
    if (activationInProgress) {
      timer = setInterval(loadStatus, 2500);
    }
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [activationInProgress, t.storageActivationFailed, t.storageStatusUnavailable]);

  useEffect(() => {
    if (step !== 2 || busy) return undefined;
    const folderName = storageState.folderName.trim();
    if (!folderName || !selectedRootPath) {
      setStorageState((current) => ({ ...current, preview: null, previewError: "" }));
      return undefined;
    }
    if (!storageState.candidateId) return undefined;
    let cancelled = false;
    const body = {
      candidate_id: usingManualRoot ? "manual" : storageState.candidateId,
      folder_name: folderName,
      manual_root_path: usingManualRoot ? storageState.manualRootPath.trim() : null,
    };
    fetch("/api/setup/storage/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(async (response) => {
        const data = await response.json().catch(() => null);
        if (cancelled) return;
        if (!response.ok) {
          setStorageState((current) => ({ ...current, preview: null, previewError: data?.detail || t.storagePreviewFailed }));
          return;
        }
        setStorageState((current) => ({ ...current, preview: data, previewError: "" }));
      })
      .catch(() => {
        if (!cancelled) setStorageState((current) => ({ ...current, preview: null, previewError: t.storagePreviewFailed }));
      });
    return () => {
      cancelled = true;
    };
  }, [busy, selectedRootPath, step, storageState.candidateId, storageState.folderName, storageState.manualRootPath, t.storagePreviewFailed, usingManualRoot]);

  function validateCurrentStep() {
    if (step === 0 && !systemNameValid) return t.required;
    if (step === 1) {
      if (!USERNAME_RE.test(form.username.trim())) return t.invalidUsername;
      if (!form.password || !form.password_confirm) return t.required;
      if (form.password !== form.password_confirm) return t.mismatch;
    }
    if (step === 2 && !storageReady) return storageDisabledReason() || t.storageBlockedReady;
    if (step === 3 && !recordingValid) return t.required;
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

  async function selectStorage() {
    const reason = storageDisabledReason();
    if (reason) {
      setStorageState((current) => ({ ...current, error: reason }));
      return;
    }
    setStorageState((current) => ({ ...current, error: "", message: "" }));
    try {
      const payload = {
        candidate_id: usingManualRoot ? "manual" : storageState.candidateId,
        folder_name: storageState.folderName.trim(),
        manual_root_path: usingManualRoot ? storageState.manualRootPath.trim() : null,
      };
      const applyResponse = await fetch("/api/setup/storage/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const applyData = await applyResponse.json().catch(() => null);
      if (!applyResponse.ok) throw new Error(applyData?.detail || t.storageActivationFailed);
      patch("storage_path", applyData?.storage_confirmation?.selected_host_path || applyData?.final_host_path || "");
      setStorageState((current) => ({
        ...current,
        confirmation: applyData.storage_confirmation || current.confirmation,
        message: t.storageSelectionQueued,
        error: "",
      }));
    } catch (err) {
      setStorageState((current) => ({ ...current, error: err?.message || t.storageActivationFailed }));
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (!systemNameValid || !ownerValid || !storageReady || !recordingValid) {
      setError(validateCurrentStep() || t.required);
      return;
    }
    setError("");
    setBusy(true);
    try {
      const response = await fetch("/api/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...form,
          username: form.username.trim(),
          system_name: form.system_name.trim() || null,
          storage_path: storageState.confirmation?.selected_host_path || "",
          language,
        }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(typeof data?.detail === "string" ? data.detail : data?.detail?.error || data?.detail?.storage?.error || data?.detail?.storage_confirmation || t.storageActivationFailed);
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
        </div>

        <div className="setupSteps" aria-label="Setup progress">
          {t.steps.map((label, index) => (
            <button
              className={`setupStep ${index === step ? "active" : ""} ${index < step ? "done" : ""}`}
              type="button"
              key={label}
              onClick={() => goToStep(index)}
              disabled={busy || !canVisitStep(index)}
            >
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
              <div className="setupIntroGrid">
                <label className="settingsField">
                  <span>{t.systemName}</span>
                  <input className="input" value={form.system_name} onChange={(e) => patch("system_name", e.target.value)} maxLength={80} />
                  <small>{t.systemNameHelp}</small>
                </label>
                <label className="settingsField">
                  <span>{t.language}</span>
                  <LanguageSelect className="select" value={language} onChange={changeLanguage} aria-label={t.language} />
                </label>
              </div>
            </section>
          ) : null}

          {step === 1 ? (
            <section className="setupPane">
              <h2>{t.ownerTitle}</h2>
              <div className="setupOwnerGrid">
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
              <div className="setupStorageGrid">
                <label className="settingsField settingsFull">
                  <span>{t.storageRootLabel}</span>
                  <select
                    className="select"
                    value={storageState.candidateId}
                    onChange={(event) => {
                      const nextId = event.target.value;
                      setStorageState((current) => ({
                        ...current,
                        candidateId: nextId,
                        preview: null,
                        previewError: "",
                        confirmation: current.confirmation?.apply_status === "active" ? current.confirmation : null,
                        message: "",
                        error: "",
                      }));
                    }}
                    disabled={storageState.loading || busy || activationInProgress}
                  >
                    {storageState.candidates.map((candidate) => (
                      <option value={candidate.id} key={candidate.id}>
                        {candidate.label}{candidate.recommended ? ` · ${t.storageRecommended}` : ""} · {t.storageFree}: {formatBytes(candidate.free_bytes)}
                      </option>
                    ))}
                    {storageState.manualPathSupported ? <option value="manual">{t.storageManualOption}</option> : null}
                  </select>
                  {selectedCandidate ? <small>{selectedCandidate.path}: {candidateText(selectedCandidate)}</small> : <small>{t.storageManualFallback}</small>}
                </label>

                {usingManualRoot ? (
                  <label className="settingsField settingsFull">
                    <span>{t.storageManualRoot}</span>
                    <input
                      className="input"
                      value={storageState.manualRootPath}
                      onChange={(event) => setStorageState((current) => ({ ...current, manualRootPath: event.target.value, preview: null, previewError: "", confirmation: null, message: "", error: "" }))}
                      placeholder={t.storageManualRootPlaceholder}
                      disabled={busy || activationInProgress}
                    />
                    <small>{t.storageManualRootHelp}</small>
                  </label>
                ) : null}

                <label className="settingsField">
                  <span>{t.storageFolder}</span>
                  <input
                    className="input"
                    value={storageState.folderName}
                    onChange={(event) => setStorageState((current) => ({ ...current, folderName: event.target.value, preview: null, previewError: "", confirmation: null, message: "", error: "" }))}
                    disabled={busy || activationInProgress}
                  />
                </label>

                <div className="settingsField setupActionField">
                  <span>{t.storageActionLabel}</span>
                  <button className="button setupPrimaryAction" type="button" onClick={selectStorage} disabled={Boolean(storageDisabledReason()) || busy}>
                    {actionLabel}
                  </button>
                </div>

                <div className="settingsStatus settingsFull compact setupStorageStatus">
                  <strong>{t.storagePreview}</strong>
                  <span>{storageState.preview?.final_host_path || storageState.confirmation?.selected_host_path || t.storageRootRequired}</span>
                  <strong>{t.storageTechnical}</strong>
                  <span>{storageState.confirmation?.container_archive_path || storageState.preview?.container_archive_path || "/storage/archive"}</span>
                  <strong>{t.status}</strong>
                  <span>{storageStatusText(storageState.confirmation?.status || storageState.preview?.status)}</span>
                  <strong>{t.nextAction}</strong>
                  <span>{nextActionText(storageState.confirmation?.next_action)}</span>
                  <strong>{t.storageFolderState}</strong>
                  <span>{storageState.preview?.exists ? t.storageFolderExists : t.storageFolderWillBeCreated}</span>
                  {selectedCandidate ? (
                    <>
                      <strong>{t.storageFree}</strong>
                      <span>{formatBytes(selectedCandidate.free_bytes)}</span>
                    </>
                  ) : null}
                  {storageState.message ? <span className="setupStatusNote">{storageState.message}</span> : null}
                  {storageState.previewError ? <span className="setupStatusError">{storageState.previewError}</span> : null}
                  {storageState.error ? <span className="setupStatusError">{storageState.error}</span> : null}
                  {!storageReady ? <span className="setupStatusNote">{storageDisabledReason() || t.storageBlockedReady}</span> : null}
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
                <span>{t.language}</span><strong>{localeMetadata(language).nativeName}</strong>
                <span>{t.username}</span><strong>{form.username.trim()}</strong>
                <span>{t.storagePreview}</span><strong>{storageState.confirmation?.selected_host_path || t.storageBlockedReady}</strong>
                <span>{t.storageTechnical}</span><strong>{storageState.confirmation?.container_archive_path || "/storage/archive"}</strong>
                <span>{t.status}</span><strong>{storageStatusText(storageState.confirmation?.status)}</strong>
                <span>{t.nextAction}</span><strong>{nextActionText(storageState.confirmation?.next_action)}</strong>
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
            <button className="button" type="button" onClick={() => goToStep(Math.min(step + 1, t.steps.length - 1))} disabled={busy || !canAdvance}>
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
