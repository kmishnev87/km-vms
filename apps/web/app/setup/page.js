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
    storageHelp: "\u041f\u0443\u0442\u044c \u0430\u0440\u0445\u0438\u0432\u0430 \u0432\u043d\u0443\u0442\u0440\u0438 \u043a\u043e\u043d\u0442\u0435\u0439\u043d\u0435\u0440\u0430. \u0425\u043e\u0441\u0442-\u043f\u0443\u0442\u044c \u0438 mount \u0437\u0430\u0434\u0430\u044e\u0442\u0441\u044f \u0432 deploy/docker; \u044d\u0442\u043e \u043f\u043e\u043b\u0435 \u043d\u0435 \u043c\u0435\u043d\u044f\u0435\u0442 runtime root.",
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
    storageHelp: "Container archive path. Host path and mount are controlled by deploy/docker; this field does not change runtime root.",
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
  const [busy, setBusy] = useState(false);
  const t = COPY[language];

  useEffect(() => {
    fetch("/api/system/status")
      .then((response) => response.ok ? response.json() : null)
      .then((status) => {
        if (status?.initialized) router.replace("/login");
      })
      .catch(() => {});
  }, [router]);

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
            <input className="input" value={form.storage_path} readOnly disabled />
            <small>{t.storageHelp}</small>
          </label>
        </div>

        {error ? <div className="authError">{error}</div> : null}
        <button className="button" type="submit" disabled={busy}>{busy ? t.busy : t.submit}</button>
      </form>
    </div>
  );
}
