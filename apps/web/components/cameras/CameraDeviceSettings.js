"use client";

import { useEffect, useId, useRef, useState } from "react";
import { apiFetch } from "../../lib/api";
import { buildOnvifConfigDiff, changeOnvifConfigField, invalidOnvifConfigField, normalizeOnvifConfigError, onvifConfigFromResult, onvifConfigTruthState, onvifConfigDisplayTruth } from "../../lib/cameraOnvifConfig";
import { OperationDialog } from "../OperationFeedback";

export default function CameraDeviceSettings({ cameraId, form, profiles, copy, onDirtyChange, onBusyChange, onVideoChanged, onSnapshot }) {
  const [opened, setOpened] = useState(false);
  const [selected, setSelected] = useState(profiles.some((profile) => profile.token === form.onvif_profile_token) ? form.onvif_profile_token : profiles[0]?.token || "");
  const [config, setConfig] = useState({ supported: {} });
  const [baseline, setOnvifConfigBaseline] = useState({ supported: {} });
  const [truth, setOnvifConfigTruth] = useState("not_checked");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pendingProfile, setPendingProfile] = useState(null);
  const currentRequest = useRef(0);
  const active = useRef(true);
  const inFlight = useRef(false);
  const automaticRead = useRef(null);
  const fieldId = useId();
  const diff = buildOnvifConfigDiff(config, baseline, config.supported);
  const dirty = Object.keys(diff).length > 0;
  const invalidField = invalidOnvifConfigField(config, baseline);
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => { onBusyChange(busy); }, [busy, onBusyChange]);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; currentRequest.current += 1; onBusyChange(false); onDirtyChange(false); };
  }, [onBusyChange, onDirtyChange]);
  useEffect(() => {
    if (!opened) { automaticRead.current = null; return; }
    if (selected && automaticRead.current !== selected) {
      automaticRead.current = selected;
      loadOnvifProfileConfig(selected);
    }
  }, [opened, selected]);

  async function request(path, token, extra = {}) {
    return apiFetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      camera_id: cameraId, host: form.host, port: Number(form.port), username: form.username, password: form.password,
      profile_token: token, ...extra,
    }) });
  }

  async function loadOnvifProfileConfig(token = selected) {
    if (!token || inFlight.current) return;
    inFlight.current = true;
    automaticRead.current = token;
    const requestId = ++currentRequest.current;
    setSelected(token); setBusy(true); setError("");
    setConfig({ supported: {} });
    setOnvifConfigBaseline({ supported: {} });
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
    } finally { if (active.current && requestId === currentRequest.current) { inFlight.current = false; setBusy(false); } }
  }

  async function applyOnvifProfileConfig() {
    if (inFlight.current || !dirty || invalidField) return;
    inFlight.current = true;
    const requestId = ++currentRequest.current;
    setBusy(true); setError("");
    try {
      const result = await request("/cameras/onvif/update_profile", selected, { config: diff });
      if (!active.current || requestId !== currentRequest.current) return;
      const snapshot = onvifConfigFromResult(result);
      setConfig(snapshot);
      setOnvifConfigBaseline(snapshot);
      setOnvifConfigTruth(result?.verification?.matched ? "verified" : "unavailable");
      onSnapshot(result);
      if (result.changed) onVideoChanged();
    } catch (err) {
      if (!active.current || requestId !== currentRequest.current) return;
      const current = err.detail?.current || err.data?.detail?.current;
      if (current) {
        const snapshot = onvifConfigFromResult(current);
        setConfig(snapshot); setOnvifConfigBaseline(snapshot); onSnapshot(current);
      }
      setOnvifConfigTruth((err.code || err.detail?.code) === "video_encoder_configuration_mismatch" ? "mismatch" : "unavailable");
      setError([401, 403].includes(err.status) ? err.message : normalizeOnvifConfigError(err, copy) || copy.profileConfigSetFailed);
      if (current || err.status === 0 || err.status >= 500 || ["video_encoder_configuration_result_unknown", "video_encoder_configuration_verify_failed"].includes(err.code)) onVideoChanged();
    } finally { if (active.current && requestId === currentRequest.current) { inFlight.current = false; setBusy(false); } }
  }

  const displayTruth = onvifConfigDisplayTruth(truth, dirty);
  const truthText = {
    not_checked: copy.profileConfigNotChecked, current: copy.profileConfigCurrent, changed: copy.profileConfigChanged,
    verified: copy.profileConfigVerified, mismatch: copy.profileConfigMismatch, unavailable: copy.profileConfigUnavailable,
    read_only: copy.profileConfigReadOnly,
  }[displayTruth];
  const slots = [
    ["codec", copy.codecCompression, false], ["codec_profile", copy.codecProfile, false],
    ["resolution", copy.resolution, false], ["fps", copy.profileConfigFps, true],
    ["bitrate_mode", copy.bitrateType, false], ["bitrate", copy.profileConfigBitrate, true],
    ["quality", copy.profileConfigQuality, true], ["iframe_interval", copy.profileConfigKeyframes, true],
  ];
  const options = profiles;
  const profileName = (profile, index) => profile.stream_kind === "main" ? copy.editorMain
    : profile.stream_kind === "sub" ? `${copy.editorSub}${profile.stream_index ? ` ${profile.stream_index}` : ""}`
      : profile.name || `${copy.profileFallback} ${index + 1}`;

  return <details className="cameraFlowAdvanced cameraFlowDevice" open={opened} onToggle={(event) => setOpened(event.currentTarget.open)}>
    <summary>{copy.editorDeviceSettings}</summary>
    {opened ? <div className="cameraFlowDeviceBody">
      <p className="cameraFlowHint">{copy.editorDeviceWarning}</p>
      <div className="cameraFlowDeviceActions">
        <label>{copy.editorDeviceProfile}
          <select className="select" value={selected} disabled={busy} onChange={(event) => {
            if (dirty) setPendingProfile(event.target.value); else loadOnvifProfileConfig(event.target.value);
          }}>
            {!options.length ? <option value="">{copy.notReceived}</option> : null}
            {options.map((profile, index) => <option key={profile.token} value={profile.token}>{profileName(profile, index)}</option>)}
          </select>
        </label>
        <button type="button" className="button secondary small" disabled={busy || !selected} onClick={() => {
          if (dirty) setPendingProfile(selected); else loadOnvifProfileConfig();
        }}>{busy ? copy.checking : copy.editorReadDevice}</button>
      </div>
      <div className={`cameraFlowDeviceFeedback ${displayTruth}`} role={error ? "alert" : "status"} aria-live="polite">{busy ? copy.checking : error || (invalidField ? copy.profileConfigInvalid : truthText)}
        {config.shared_profile_tokens?.length > 1 ? <span className="cameraFlowSharedEncoder">{copy.profileConfigShared}</span> : null}
      </div>
      <div className="cameraFlowSettings">
        {slots.map(([key, label, numeric]) => {
          const meta = config.supported?.[key] || {};
          const value = config[key] ?? "";
          const singleOption = meta.options?.length === 1 || meta.range?.min != null && meta.range.min === meta.range.max;
          const writable = meta.writable === true && !singleOption && truth !== "unavailable" && (key !== "codec" || meta.options?.length);
          const change = (event) => {
            setConfig((previous) => changeOnvifConfigField(previous, key, event.target.value));
            setOnvifConfigTruth("changed"); setError("");
          };
          const note = writable ? meta.range?.min != null && meta.range?.max != null
            ? `${meta.range.min}–${meta.range.max}` : copy.profileConfigEditable
            : meta.readable ? singleOption ? copy.profileConfigSingleOption : copy.readonly : copy.profileConfigNotProvided;
          return <label key={key}><span id={`${fieldId}-${key}`}>{label}</span><span className="cameraFlowFieldNote">{note}</span>
            {writable ? meta.options?.length
              ? <select className="select" value={value} onChange={change} disabled={busy} aria-labelledby={`${fieldId}-${key}`}>
                  {!meta.options.some((option) => String(option) === String(value)) ? <option value={value}>{value || copy.notReceived}</option> : null}
                  {meta.options.map((option) => <option key={option} value={option}>{option}</option>)}
                </select>
              : <input className="input" type={numeric ? "number" : "text"} min={meta.range?.min} max={meta.range?.max}
                  step={meta.range?.step || (key === "quality" || key === "fps" ? "any" : 1)} value={value} onChange={change} disabled={busy} aria-labelledby={`${fieldId}-${key}`} aria-invalid={invalidField === key} />
              : <span className="cameraFlowReadonly">{value === "" ? copy.notReceived : value}</span>}
          </label>;
        })}
      </div>
      <div className="cameraFlowDeviceActions">
        <button type="button" className="button secondary small" disabled={busy || !dirty || Boolean(invalidField)} onClick={applyOnvifProfileConfig}>{busy ? copy.applying : copy.editorApplyDevice}</button>
        <button type="button" className="cameraFlowTextButton" disabled={busy || !dirty} onClick={() => {
          setConfig(baseline); setOnvifConfigTruth("current"); setError("");
        }}>{copy.editorDiscardDevice}</button>
      </div>
    </div> : null}
    <OperationDialog dialog={pendingProfile !== null ? {
      id: "camera-device-draft", presentation: "compact-confirmation", title: copy.editorDeviceUnsaved,
      message: copy.editorDeviceDiscardQuestion, confirmLabel: copy.editorDiscardContinue, cancelLabel: copy.editorKeepEditing,
      tone: "warning", onConfirm: () => { const token = pendingProfile; setPendingProfile(null); loadOnvifProfileConfig(token); },
    } : null} onClose={() => setPendingProfile(null)} />
  </details>;
}
