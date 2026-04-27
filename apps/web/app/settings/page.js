"use client";

import { useEffect, useState } from "react";
import Layout from "../../components/Layout";
import { apiFetch } from "../../lib/api";

const TEXT = {
  ru: {
    title: "\u0410\u0434\u043c\u0438\u043d / \u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438",
    subtitle: "KM VMS Control Plane: \u0441\u0438\u0441\u0442\u0435\u043c\u0430, \u0445\u0440\u0430\u043d\u0438\u043b\u0438\u0449\u0435, \u0437\u0430\u043f\u0438\u0441\u044c, hardware \u0438 \u0441\u0435\u0441\u0441\u0438\u0438.",
    save: "\u0421\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c",
    validate: "\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u0445\u0440\u0430\u043d\u0438\u043b\u0438\u0449\u0435",
    rescan: "\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u0430\u043f\u043f\u0430\u0440\u0430\u0442\u043d\u044b\u0435 \u0432\u043e\u0437\u043c\u043e\u0436\u043d\u043e\u0441\u0442\u0438",
    system: "\u0421\u0438\u0441\u0442\u0435\u043c\u0430",
    storage: "\u0425\u0440\u0430\u043d\u0438\u043b\u0438\u0449\u0435",
    recording: "\u0417\u0430\u043f\u0438\u0441\u044c",
    hardware: "Hardware",
    security: "\u0411\u0435\u0437\u043e\u043f\u0430\u0441\u043d\u043e\u0441\u0442\u044c",
    language: "\u042f\u0437\u044b\u043a",
    timezone: "\u0427\u0430\u0441\u043e\u0432\u043e\u0439 \u043f\u043e\u044f\u0441",
    storagePath: "\u041f\u0443\u0442\u044c \u0445\u0440\u0430\u043d\u0438\u043b\u0438\u0449\u0430",
    recordingFormat: "\u0424\u043e\u0440\u043c\u0430\u0442 \u0437\u0430\u043f\u0438\u0441\u0438",
    preferredBackend: "\u041f\u0440\u0435\u0434\u043f\u043e\u0447\u0442\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0439 backend",
    formatNote: "MKV: \u0432\u044b\u0448\u0435 \u0443\u0441\u0442\u043e\u0439\u0447\u0438\u0432\u043e\u0441\u0442\u044c \u043a \u0441\u0431\u043e\u044f\u043c. MP4: \u0448\u0438\u0440\u0435 \u0441\u043e\u0432\u043c\u0435\u0441\u0442\u0438\u043c\u043e\u0441\u0442\u044c, \u043d\u043e \u0445\u0440\u0443\u043f\u0447\u0435 \u043f\u0440\u0438 \u0430\u0432\u0430\u0440\u0438\u0439\u043d\u043e\u043c \u043e\u0431\u0440\u044b\u0432\u0435.",
    securityNote: "\u00ab\u041e\u0441\u0442\u0430\u0432\u0430\u0442\u044c\u0441\u044f \u0432 \u0441\u0438\u0441\u0442\u0435\u043c\u0435\u00bb \u0445\u0440\u0430\u043d\u0438\u0442 \u0441\u0435\u0441\u0441\u0438\u044e \u0442\u043e\u043b\u044c\u043a\u043e \u0434\u043e 24:00 \u0441\u0438\u0441\u0442\u0435\u043c\u043d\u043e\u0433\u043e \u0434\u043d\u044f. \u0411\u0435\u0437 \u044d\u0442\u043e\u0433\u043e \u0441\u0435\u0441\u0441\u0438\u044f \u0436\u0438\u0432\u0451\u0442 \u0432 \u0440\u0430\u043c\u043a\u0430\u0445 browser session.",
    rolesNote: "\u0420\u043e\u043b\u0438: admin, operator, viewer. Permissions foundation enforced by backend settings APIs.",
    saved: "\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438 \u0441\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u044b",
  },
  en: {
    title: "Admin / Settings",
    subtitle: "KM VMS Control Plane: system, storage, recording, hardware and sessions.",
    save: "Save",
    validate: "Validate storage",
    rescan: "Check hardware capabilities",
    system: "System",
    storage: "Storage",
    recording: "Recording",
    hardware: "Hardware",
    security: "Security",
    language: "Language",
    timezone: "Timezone",
    storagePath: "Storage path",
    recordingFormat: "Recording format",
    preferredBackend: "Preferred backend",
    formatNote: "MKV: higher crash-safety/reliability. MP4: broader compatibility, but more fragile on abrupt termination.",
    securityNote: "Stay signed in stores the session only until 24:00 of the configured system day. Without it, auth is browser-session scoped.",
    rolesNote: "Roles: admin, operator, viewer. Permissions foundation is enforced by backend settings APIs.",
    saved: "Settings saved",
  },
};

export default function SettingsPage() {
  const [settings, setSettings] = useState(null);
  const [hardware, setHardware] = useState(null);
  const [storageResult, setStorageResult] = useState(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const t = TEXT[settings?.language || "ru"] || TEXT.ru;
  const languageIcon = settings?.language === "en"
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
      setHardware(hardwareData);
    } catch (err) {
      setError(err?.message || "Failed to load settings");
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
          recording_format: settings.recording_format,
          hardware_preferred_backend: settings.hardware_preferred_backend || null,
        }),
      });
      setSettings(updated);
      setMessage(t.saved);
    } catch (err) {
      setError(err?.message || "Failed to save settings");
    } finally {
      setBusy(false);
    }
  }

  async function validateStorage() {
    setStorageResult(null);
    setError("");
    try {
      const result = await apiFetch("/settings/storage/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ storage_path: settings.storage_path, create: true }),
      });
      setStorageResult(result);
    } catch (err) {
      setError(err?.message || "Storage validation failed");
    }
  }

  async function rescanHardware() {
    setError("");
    try {
      setHardware(await apiFetch("/hardware/rescan", { method: "POST" }));
    } catch (err) {
      setError(err?.message || "Hardware rescan failed");
    }
  }

  return (
    <Layout>
      <div className="settingsPage">
        <div className="pageHeader">
          <div>
            <h1 className="pageTitle">{t.title}</h1>
            <div className="pageSubtitle">{t.subtitle}</div>
          </div>
          <button className="button small" onClick={save} disabled={!settings || busy}>{t.save}</button>
        </div>

        {error ? <div className="settingsAlert error">{error}</div> : null}
        {message ? <div className="settingsAlert ok">{message}</div> : null}

        {!settings ? null : (
          <div className="settingsSections">
            <section className="settingsSection">
              <h2 className="settingsSectionTitle">
                <img src="/icons/nav/settings-icon.png" alt="" />
                <span>{t.system}</span>
              </h2>
              <div className="settingsGrid">
                <label className="settingsField">
                  <span className="settingsFieldLabel"><img src={languageIcon} alt="" />{t.language}</span>
                  <select className="select" value={settings.language} onChange={(e) => patch("language", e.target.value)}>
                    <option value="ru">RU</option>
                    <option value="en">EN</option>
                  </select>
                </label>
                <label className="settingsField">
                  <span className="settingsFieldLabel"><img src="/icons/nav/timezone-icon.png" alt="" />{t.timezone}</span>
                  <input className="input" value={settings.timezone || ""} onChange={(e) => patch("timezone", e.target.value)} />
                </label>
              </div>
            </section>

            <section className="settingsSection">
              <h2 className="settingsSectionTitle">
                <img src="/icons/nav/storage-icon.png" alt="" />
                <span>{t.storage}</span>
              </h2>
              <div className="settingsGrid">
                <label className="settingsField settingsFull">
                  <span>{t.storagePath}</span>
                  <input className="input" value={settings.storage_path || ""} onChange={(e) => patch("storage_path", e.target.value)} />
                </label>
              </div>
              <button className="button secondary small" onClick={validateStorage}>{t.validate}</button>
              {storageResult ? (
                <pre className={`settingsJson ${storageResult.ok ? "ok" : "error"}`}>{JSON.stringify(storageResult, null, 2)}</pre>
              ) : null}
            </section>

            <section className="settingsSection">
              <h2 className="settingsSectionTitle">
                <img src="/icons/nav/storage-icon.png" alt="" />
                <span>{t.recording}</span>
              </h2>
              <div className="settingsGrid">
                <label className="settingsField">
                  <span>{t.recordingFormat}</span>
                  <select className="select" value={settings.recording_format} onChange={(e) => patch("recording_format", e.target.value)}>
                    <option value="mkv">MKV</option>
                    <option value="mp4">MP4</option>
                  </select>
                </label>
              </div>
              <div className="settingsNote">{t.formatNote}</div>
            </section>

            <section className="settingsSection">
              <h2 className="settingsSectionTitle">
                <img src="/icons/nav/hardware-icon.png" alt="" />
                <span>{t.hardware}</span>
              </h2>
              <button className="button secondary small" onClick={rescanHardware}>{t.rescan}</button>
              <div className="settingsGrid settingsHardwareGrid">
                <label className="settingsField">
                  <span>{t.preferredBackend}</span>
                  <select className="select" value={settings.hardware_preferred_backend || ""} onChange={(e) => patch("hardware_preferred_backend", e.target.value || null)}>
                    <option value="">Auto</option>
                    {(hardware?.available_backends || []).map((backend) => <option key={backend} value={backend}>{backend}</option>)}
                  </select>
                </label>
              </div>
              {hardware ? <pre className="settingsJson">{JSON.stringify(hardware, null, 2)}</pre> : null}
            </section>

            <section className="settingsSection">
              <h2 className="settingsSectionTitle">
                <img src="/icons/nav/security-icon.png" alt="" />
                <span>{t.security}</span>
              </h2>
              <div className="settingsNote">{t.securityNote}</div>
              <div className="settingsNote settingsUsersNote">
                <img src="/icons/nav/users-icon.png" alt="" />
                <span>{t.rolesNote}</span>
              </div>
            </section>
          </div>
        )}
      </div>
    </Layout>
  );
}
