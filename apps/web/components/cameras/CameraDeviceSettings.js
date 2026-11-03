"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../../lib/api";
import { buildOnvifConfigDiff, normalizeOnvifConfigError, onvifConfigFromResult, onvifConfigTruthState, onvifConfigDisplayTruth } from "../../lib/cameraOnvifConfig";
import { OperationDialog } from "../OperationFeedback";

export default function CameraDeviceSettings({ cameraId, form, profiles, copy, onDirtyChange, onBusyChange, onVideoChanged, onSnapshot }) {
  const [opened, setOpened] = useState(false);
  const [selected, setSelected] = useState(form.onvif_profile_token || profiles[0]?.token || "");
  const [config, setConfig] = useState({ supported: {} });
  const [baseline, setOnvifConfigBaseline] = useState({ supported: {} });
  const [truth, setOnvifConfigTruth] = useState("not_checked");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pendingProfile, setPendingProfile] = useState(null);
  const currentRequest = useRef(0);
  const active = useRef(true);
  const diff = buildOnvifConfigDiff(config, baseline, config.supported);
  const dirty = Object.keys(diff).length > 0;
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => { onBusyChange(busy); }, [busy, onBusyChange]);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; currentRequest.current += 1; onBusyChange(false); onDirtyChange(false); };
  }, [onBusyChange, onDirtyChange]);

  async function request(path, token, extra = {}) {
    return apiFetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      camera_id: cameraId, host: form.host, port: Number(form.port), username: form.username, password: form.password,
      profile_token: token, ...extra,
    }) });
  }

  async function loadOnvifProfileConfig(token = selected) {
    if (!token || busy) return;
    const requestId = ++currentRequest.current;
    const sameProfile = token === selected;
    setSelected(token); setBusy(true); setError("");
    if (!sameProfile) setConfig({ supported: {} });
    setOnvifConfigTruth("not_checked");
    try {
      const result = await request("/cameras/onvif/profile_config", token);
      if (!active.current || requestId !== currentRequest.current) return;
      const snapshot = onvifConfigFromResult(result);
      setConfig(snapshot);
      setOnvifConfigBaseline(snapshot);
      setOnvifConfigTruth(onvifConfigTruthState(result));
      onSnapshot(result);
    } catch (err) {
      if (!active.current || requestId !== currentRequest.current) return;
      setOnvifConfigTruth("unavailable");
      setError([401, 403].includes(err.status) ? err.message : normalizeOnvifConfigError(err, copy) || copy.profileConfigUnavailable);
    } finally { if (active.current && requestId === currentRequest.current) setBusy(false); }
  }

  async function applyOnvifProfileConfig() {
    if (busy || !dirty) return;
    const requestId = ++currentRequest.current;
    setBusy(true); setError(""); onVideoChanged();
    try {
      const result = await request("/cameras/onvif/update_profile", selected, { config: diff });
      if (!active.current || requestId !== currentRequest.current) return;
      const snapshot = onvifConfigFromResult(result);
      setConfig(snapshot);
      setOnvifConfigBaseline(snapshot);
      setOnvifConfigTruth(result?.verification?.matched ? "verified" : "unavailable");
      onSnapshot(result);
    } catch (err) {
      if (!active.current || requestId !== currentRequest.current) return;
      setOnvifConfigTruth((err.code || err.detail?.code) === "video_encoder_configuration_mismatch" ? "mismatch" : "unavailable");
      setError([401, 403].includes(err.status) ? err.message : normalizeOnvifConfigError(err, copy) || copy.profileConfigSetFailed);
    } finally { if (active.current && requestId === currentRequest.current) setBusy(false); }
  }

  const displayTruth = onvifConfigDisplayTruth(truth, dirty);
  const truthText = {
    not_checked: copy.profileConfigNotChecked, current: copy.profileConfigCurrent, changed: copy.profileConfigChanged,
    verified: copy.profileConfigVerified, mismatch: copy.profileConfigMismatch, unavailable: copy.profileConfigUnavailable,
  }[displayTruth];
  const slots = [
    ["codec", copy.codecCompression, false], ["resolution", copy.resolution, false],
    ["fps", copy.fps, true], ["bitrate", copy.maxBitrate, true],
    ["iframe_interval", copy.iframeInterval, true], ["quality", copy.vbrQuality, true],
  ];
  const options = profiles.length ? profiles : selected ? [{ token: selected, name: copy.editorSavedProfile }] : [];

  return <details className="cameraFlowAdvanced cameraFlowDevice" open={opened} onToggle={(event) => setOpened(event.currentTarget.open)}>
    <summary>{copy.editorDeviceSettings}<span>{copy.editorOptional}</span></summary>
    {opened ? <div className="cameraFlowDeviceBody">
      <p className="cameraFlowHint">{copy.editorDeviceWarning}</p>
      <div className="cameraFlowDeviceActions">
        <label>{copy.editorDeviceProfile}
          <select className="select" value={selected} disabled={busy} onChange={(event) => {
            if (dirty) setPendingProfile(event.target.value); else loadOnvifProfileConfig(event.target.value);
          }}>
            {options.map((profile, index) => <option key={profile.token} value={profile.token}>{profile.name || `${copy.profileFallback} ${index + 1}`}</option>)}
          </select>
        </label>
        <button type="button" className="button secondary small" disabled={busy || !selected} onClick={() => {
          if (dirty) setPendingProfile(selected); else loadOnvifProfileConfig();
        }}>{busy ? copy.checking : copy.editorReadDevice}</button>
      </div>
      <div className={`cameraFlowDeviceFeedback ${displayTruth}`} role={error ? "alert" : "status"} aria-live="polite">{busy ? copy.checking : error || truthText}</div>
      <div className="cameraFlowSettings">
        {slots.filter(([key]) => config.supported?.[key]?.readable || config[key] !== undefined && config[key] !== "").map(([key, label, numeric]) => {
          const meta = config.supported?.[key] || {};
          const value = config[key] ?? "";
          const writable = meta.writable === true && (key !== "codec" || meta.options?.length);
          const change = (event) => {
            setConfig((previous) => ({ ...previous, [key]: event.target.value }));
            setOnvifConfigTruth("changed"); setError("");
          };
          return <label key={key}>{label}<span className="cameraFlowFieldNote">{writable ? copy.writable : copy.readonly}</span>
            {writable ? meta.options?.length
              ? <select className="select" value={value} onChange={change} disabled={busy}>
                  {!meta.options.some((option) => String(option) === String(value)) ? <option value={value}>{value || copy.notReceived}</option> : null}
                  {meta.options.map((option) => <option key={option} value={option}>{option}</option>)}
                </select>
              : <input className="input" type={numeric ? "number" : "text"} min={meta.range?.min} max={meta.range?.max} value={value} onChange={change} disabled={busy} />
              : <span className="cameraFlowReadonly">{value || copy.notReceived}</span>}
          </label>;
        })}
      </div>
      <div className="cameraFlowDeviceActions">
        <button type="button" className="button secondary small" disabled={busy || !dirty} onClick={applyOnvifProfileConfig}>{busy ? copy.applying : copy.editorApplyDevice}</button>
        {dirty ? <button type="button" className="cameraFlowTextButton" disabled={busy} onClick={() => {
          setConfig(baseline); setOnvifConfigTruth("current"); setError("");
        }}>{copy.editorDiscardDevice}</button> : null}
      </div>
    </div> : null}
    <OperationDialog dialog={pendingProfile !== null ? {
      id: "camera-device-draft", presentation: "compact-confirmation", title: copy.editorDeviceUnsaved,
      message: copy.editorDeviceDiscardQuestion, confirmLabel: copy.editorDiscardContinue, cancelLabel: copy.editorKeepEditing,
      tone: "warning", onConfirm: () => { const token = pendingProfile; setPendingProfile(null); loadOnvifProfileConfig(token); },
    } : null} onClose={() => setPendingProfile(null)} />
  </details>;
}
