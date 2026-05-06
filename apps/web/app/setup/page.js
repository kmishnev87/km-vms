"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

const COPY = {
  ru: {
    title: "\u041f\u0435\u0440\u0432\u0438\u0447\u043d\u0430\u044f \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0430 KM VMS",
    subtitle: "\u0421\u043e\u0437\u0434\u0430\u0439\u0442\u0435 \u043f\u0435\u0440\u0432\u043e\u0433\u043e \u0430\u0434\u043c\u0438\u043d\u0430 \u0438 \u0431\u0430\u0437\u043e\u0432\u044b\u0435 \u0441\u0438\u0441\u0442\u0435\u043c\u043d\u044b\u0435 \u043f\u0430\u0440\u0430\u043c\u0435\u0442\u0440\u044b.",
    admin: "\u0410\u0434\u043c\u0438\u043d",
    password: "\u041f\u0430\u0440\u043e\u043b\u044c",
    timezone: "\u0427\u0430\u0441\u043e\u0432\u043e\u0439 \u043f\u043e\u044f\u0441",
    language: "\u042f\u0437\u044b\u043a",
    storage: "\u041f\u0443\u0442\u044c \u0445\u0440\u0430\u043d\u0438\u043b\u0438\u0449\u0430",
    storageHelp: "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 NAS/server \u0434\u0438\u0441\u043a \u0438 \u043f\u0430\u043f\u043a\u0443 \u0430\u0440\u0445\u0438\u0432\u0430. \u0412\u043d\u0443\u0442\u0440\u0438 Docker \u043f\u0443\u0442\u044c \u043e\u0441\u0442\u0430\u0435\u0442\u0441\u044f /storage/archive.",
    storageFolder: "\u041f\u0430\u043f\u043a\u0430 \u0430\u0440\u0445\u0438\u0432\u0430",
    storagePreview: "\u0418\u0442\u043e\u0433\u043e\u0432\u044b\u0439 NAS/server \u043f\u0443\u0442\u044c",
    storageTechnical: "\u0422\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u0439 Docker \u043f\u0443\u0442\u044c",
    storageUnavailable: "\u0412\u044b\u0431\u043e\u0440 \u0434\u0438\u0441\u043a\u0430 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d: \u043d\u0443\u0436\u0435\u043d host snapshot \u043e\u0442 installer.",
    storageApply: "\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u0438 \u0432\u044b\u0431\u0440\u0430\u0442\u044c",
    storageAllowed: "\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d",
    storageBlocked: "\u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d",
    storageWritable: "\u0437\u0430\u043f\u0438\u0441\u044c",
    storageReadOnly: "\u0442\u043e\u043b\u044c\u043a\u043e \u0447\u0442\u0435\u043d\u0438\u0435",
    storageTotal: "\u0432\u0441\u0435\u0433\u043e",
    storageUsed: "\u0437\u0430\u043d\u044f\u0442\u043e",
    storageFree: "\u0441\u0432\u043e\u0431\u043e\u0434\u043d\u043e",
    storageReason: "\u043f\u0440\u0438\u0447\u0438\u043d\u0430",
    format: "\u0424\u043e\u0440\u043c\u0430\u0442 \u0437\u0430\u043f\u0438\u0441\u0438",
    formatHelp: "MKV = \u043d\u0430\u0434\u0435\u0436\u043d\u043e\u0441\u0442\u044c, MP4 = \u0441\u043e\u0432\u043c\u0435\u0441\u0442\u0438\u043c\u043e\u0441\u0442\u044c.",
    submit: "\u0417\u0430\u0432\u0435\u0440\u0448\u0438\u0442\u044c \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0443",
    busy: "\u0421\u043e\u0445\u0440\u0430\u043d\u044f\u0435\u043c...",
  },
  en: {
    title: "KM VMS first-run setup",
    subtitle: "Create the first administrator and baseline system settings.",
    admin: "Admin login",
    password: "Password",
    timezone: "Timezone",
    language: "Language",
    storage: "Storage path",
    storageHelp: "Choose the NAS/server disk and archive folder. The Docker path remains /storage/archive.",
    storageFolder: "Archive folder",
    storagePreview: "Final NAS/server path",
    storageTechnical: "Technical Docker path",
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
    format: "Recording format",
    formatHelp: "MKV = reliability, MP4 = compatibility.",
    submit: "Finish setup",
    busy: "Saving...",
  },
};

export default function SetupPage() {
  const router = useRouter();
  const [language, setLanguage] = useState("ru");
  const [form, setForm] = useState({
    username: "admin",
    password: "",
    timezone: "Asia/Yekaterinburg",
    storage_path: "/storage/archive",
    recording_format: "mkv",
  });
  const [error, setError] = useState("");
  const [storageState, setStorageState] = useState({
    loading: true,
    candidates: [],
    candidateId: "",
    folderName: "KM-VMS-Recordings",
    preview: null,
    message: "",
    error: "",
  });
  const [busy, setBusy] = useState(false);
  const t = COPY[language];

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
    const status = candidate.safety_status === "allowed" ? t.storageAllowed : t.storageBlocked;
    return [
      `${t.storageTotal}: ${formatBytes(candidate.total_bytes)}`,
      `${t.storageUsed}: ${formatBytes(candidate.used_bytes)}`,
      `${t.storageFree}: ${formatBytes(candidate.free_bytes)}${freePercent === null ? "" : ` (${freePercent}%)`}`,
      candidate.writable ? t.storageWritable : t.storageReadOnly,
      status,
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
        const first = allowed[0]?.id || "";
        setStorageState((current) => ({
          ...current,
          loading: false,
          candidates,
          candidateId: first,
          error: allowed.length ? "" : t.storageUnavailable,
        }));
      })
      .catch(() => setStorageState((current) => ({ ...current, loading: false, error: t.storageUnavailable })));
  }, [t.storageUnavailable]);

  function patch(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function submit(e) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const response = await fetch("/api/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...form, language }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(typeof data?.detail === "string" ? data.detail : JSON.stringify(data?.detail || data || response.status));
      }
      router.replace("/login");
    } catch (err) {
      setError(err?.message || "Setup failed");
    } finally {
      setBusy(false);
    }
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
      patch("storage_path", applyData.final_host_path);
      setStorageState((current) => ({
        ...current,
        preview: applyData,
        message: applyData.apply_status || "selected",
      }));
    } catch (err) {
      setStorageState((current) => ({ ...current, error: err?.message || "Storage selection failed" }));
    }
  }

  return (
    <div className="setupPage">
      <form className="setupCard" onSubmit={submit}>
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

        <div className="settingsGrid">
          <label className="settingsField">
            <span>{t.admin}</span>
            <input className="input" value={form.username} onChange={(e) => patch("username", e.target.value)} autoComplete="username" />
          </label>
          <label className="settingsField">
            <span>{t.password}</span>
            <input className="input" type="password" value={form.password} onChange={(e) => patch("password", e.target.value)} autoComplete="new-password" />
          </label>
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
          <label className="settingsField settingsFull">
            <span>{t.storage}</span>
            <select
              className="select"
              value={storageState.candidateId}
              onChange={(e) => setStorageState((current) => ({ ...current, candidateId: e.target.value, preview: null }))}
              disabled={storageState.loading || !storageState.candidates.some((candidate) => candidate.safety_status === "allowed")}
            >
              {storageState.candidates.map((candidate) => (
                <option value={candidate.id} key={candidate.id} disabled={candidate.safety_status !== "allowed"}>
                  {candidate.label} - {candidate.safety_status === "allowed" ? t.storageAllowed : t.storageBlocked} - {t.storageFree}: {formatBytes(candidate.free_bytes)}
                </option>
              ))}
            </select>
            {storageState.candidates.length ? (
              <div className="settingsStatus">
                {storageState.candidates.map((candidate) => (
                  <small key={`details-${candidate.id}`}>
                    {candidate.path}: {candidateText(candidate)}
                  </small>
                ))}
              </div>
            ) : null}
            <span>{t.storageFolder}</span>
            <input
              className="input"
              value={storageState.folderName}
              onChange={(e) => setStorageState((current) => ({ ...current, folderName: e.target.value, preview: null }))}
              disabled={!storageState.candidates.length}
            />
            <button className="button secondary small" type="button" onClick={selectStorage} disabled={!storageState.candidateId || busy}>
              {t.storageApply}
            </button>
            <input className="input" value={form.storage_path} readOnly disabled />
            {storageState.preview ? (
              <small>{t.storagePreview}: {storageState.preview.final_host_path}. {t.storageTechnical}: {storageState.preview.container_archive_path}</small>
            ) : null}
            {storageState.message ? <small>{storageState.message}</small> : null}
            {storageState.error ? <small>{storageState.error}</small> : null}
            <small>{t.storageHelp}</small>
          </label>
        </div>

        {error ? <div className="authError">{error}</div> : null}
        <button className="button" type="submit" disabled={busy}>{busy ? t.busy : t.submit}</button>
      </form>
    </div>
  );
}
