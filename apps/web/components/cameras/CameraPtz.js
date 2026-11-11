"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { apiFetch, captureAuthSession } from "../../lib/api";
import { usePtzController } from "../../lib/useCameraPtz";
import { useI18n } from "../../lib/i18n";
import { ptzSpeedLevel } from "../../lib/ptzUiGeometry";
import TilePlayer from "../TilePlayer";
import { OperationDialog, OperationToast } from "../OperationFeedback";

export function PtzIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 3v18M3 12h18M9 6l3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3" /></svg>;
}

export function CameraPtzBadge({ camera, capability, onClick, expanded = false, controls }) {
  const { t } = useI18n();
  if (!capability?.supported) return null;
  return <button type="button" className="cameraPtzBadge" data-ptz-camera-badge={camera.id}
    title={t("ptz.open")} aria-label={`${t("ptz.open")}: ${camera.name}`}
    aria-expanded={expanded} aria-controls={controls}
    onPointerDown={event => event.stopPropagation()}
    onPointerUp={event => event.stopPropagation()}
    onDoubleClick={event => event.stopPropagation()}
    onClick={event => { event.stopPropagation(); onClick?.(); }}>
    <PtzIcon />
  </button>;
}

const DIRECTIONS = [
  ["up_left", "↖"], ["up", "↑"], ["up_right", "↗"],
  ["left", "←"], ["stop", "■"], ["right", "→"],
  ["down_left", "↙"], ["down", "↓"], ["down_right", "↘"],
];

function PtzToast({ toast, onClose }) {
  const [host, setHost] = useState(null);
  useEffect(() => {
    const syncHost = () => setHost(document.fullscreenElement || document.body);
    syncHost();
    document.addEventListener("fullscreenchange", syncHost);
    return () => document.removeEventListener("fullscreenchange", syncHost);
  }, []);
  // Panels can establish a clipped containing block (especially backdrop-filter
  // in fullscreen). Reuse the app's centered toast outside that panel instead.
  return host ? createPortal(<OperationToast toast={toast} onClose={onClose} />, host) : null;
}

export function CameraPtzControls({ camera, capability, overlay = false, onClose,
  speed: selectedSpeed, onSpeedChange, pointActive = false, onPointModeChange,
  areaZoomActive = false, onAreaZoomModeChange }) {
  const { t } = useI18n();
  const [localSpeed, setLocalSpeed] = useState(2);
  const speed = selectedSpeed ?? localSpeed;
  const setSpeed = value => { setLocalSpeed(value); onSpeedChange?.(value); };
  const speedPointer = useRef(null);
  const [saved, setSaved] = useState([]);
  const [selected, setSelected] = useState("");
  const [presetsState, setPresetsState] = useState("loading");
  const [presetBusy, setPresetBusy] = useState(false);
  const [toast, setToast] = useState(null);
  const pressedPointer = useRef(null);
  const alive = useRef(true);
  const { controller, held } = usePtzController(camera.id, capability?.profile_token, () => {
    setToast({ id: `ptz-${Date.now()}`, title: t("ptz.title"), message: t("ptz.commandFailed"), tone: "error" });
  });
  useEffect(() => {
    let active = true;
    alive.current = true;
    const ownSession = captureAuthSession();
    setSaved([]); setSelected(""); setPresetsState("loading");
    if (capability?.can_presets === false) {
      setPresetsState("unavailable");
      return () => { active = false; alive.current = false; };
    }
    apiFetch(`/cameras/${camera.id}/onvif/ptz/presets`).then(result => {
      if (!active || !ownSession()) return;
      setSaved(Array.isArray(result?.presets) ? result.presets : []);
      setPresetsState("loaded");
    }).catch(() => {
      if (!active || !ownSession()) return;
      setPresetsState("failed");
      setToast({ id: `ptz-presets-${Date.now()}`, title: t("ptz.title"), message: t("ptz.presetsFailed"), tone: "error" });
    });
    return () => { active = false; alive.current = false; };
  }, [camera.id, capability?.profile_token, capability?.can_presets]);

  function finish(event) {
    event?.stopPropagation();
    if (event?.pointerId != null && pressedPointer.current !== event.pointerId) return;
    pressedPointer.current = null;
    controller.release();
  }
  function cancel(event) {
    event?.stopPropagation();
    if (event?.pointerId != null && pressedPointer.current !== event.pointerId) return;
    pressedPointer.current = null;
    void controller.stop();
  }
  function start(event, action, direction) {
    if (event.button !== 0 || event.isPrimary === false) return;
    event.preventDefault(); event.stopPropagation();
    pressedPointer.current = event.pointerId;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    controller.press(action, direction, [.04, .08, .12, .2, .3][speed - 1]);
  }
  function keyboard(event, action, direction) {
    if (![" ", "Enter"].includes(event.key)) return;
    event.preventDefault(); event.stopPropagation();
    if (!event.repeat) controller.press(action, direction, [.04, .08, .12, .2, .3][speed - 1]);
  }
  function movementButton(action, direction, icon, label) {
    const pressed = held?.action === action && held?.direction === direction;
    return <button type="button" key={direction} className={`ptzDirection ${pressed ? "isPressed" : ""}`}
      data-ptz-direction={direction} aria-label={label} title={label} disabled={presetBusy}
      onPointerDown={event => start(event, action, direction)} onPointerUp={finish}
      onPointerCancel={cancel} onLostPointerCapture={cancel}
      onKeyDown={event => keyboard(event, action, direction)}
      onKeyUp={event => { if ([" ", "Enter"].includes(event.key)) finish(event); }}
      onBlur={() => { void controller.stop(); }}>
      {icon}
    </button>;
  }
  async function go(token) {
    setSelected(token);
    if (!token || presetBusy) return;
    setPresetBusy(true);
    await controller.gotoPreset(token);
    if (!alive.current) return;
    setPresetBusy(false);
    // This is a command picker, not a claim that the camera is still at that
    // position. Reset it so the same preset can be selected again after moving.
    setSelected("");
  }
  const showPresets = capability?.can_presets !== false || saved.length > 0;
  return <section className={`cameraPtzControls ${overlay ? "cameraPtzControlsOverlay" : ""}`}
    data-ptz-controls={camera.id} aria-label={`${t("ptz.title")}: ${camera.name}`}
    onPointerDown={event => event.stopPropagation()} onPointerUp={event => event.stopPropagation()}
    onWheel={event => event.stopPropagation()}
    onDoubleClick={event => event.stopPropagation()} onDragStart={event => { event.preventDefault(); event.stopPropagation(); }}>
    <div className="ptzControlHeader"><span>{t("ptz.title")}</span><div className="ptzHeaderActions">
      {capability?.can_point && onPointModeChange ? <button type="button" className="ptzPointToggle"
        aria-label={t("ptz.pointMode")} title={t("ptz.pointMode")} aria-pressed={pointActive}
        onClick={() => { void controller.stop(); onPointModeChange(!pointActive); }}>
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="6" /><path d="M12 2v6m0 8v6M2 12h6m8 0h6" /></svg>
      </button> : null}
      {onClose ? <button type="button" className="ptzClose"
        aria-label={t("ptz.close")} onClick={() => { void controller.stop(); onClose(); }}>×</button> : null}
    </div></div>
    {capability?.can_pan_tilt ? <div className="ptzDirectionPad">
      {DIRECTIONS.map(([direction, icon]) => direction === "stop"
        ? <button type="button" key="stop" className="ptzDirection ptzStop" aria-label={t("ptz.stop")}
          title={t("ptz.stop")} onClick={() => { void controller.stop(true); }}>{icon}</button>
        : movementButton("move", direction, icon, t(`ptz.${direction}`)))}
    </div> : <button type="button" className="ptzStopOnly" onClick={() => { void controller.stop(true); }}>{t("ptz.stop")}</button>}
    {capability?.can_zoom || capability?.can_area_zoom ? <div className="ptzZoom">
      {capability?.can_zoom ? movementButton("zoom", "out", "−", t("ptz.zoomOut")) : <span />}
      <span className="ptzZoomLabel">{t("ptz.zoom")}
        {capability?.can_area_zoom && onAreaZoomModeChange ? <button type="button" className="ptzPointToggle"
          data-ptz-area-toggle="true" aria-label={t("ptz.areaMode")} title={t("ptz.areaHint")}
          aria-pressed={areaZoomActive} onClick={() => { void controller.stop(); onAreaZoomModeChange(!areaZoomActive); }}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 8V3h5m8 0h5v5M3 16v5h5m8 0h5v-5" />
            <circle cx="11" cy="11" r="4" /><path d="m14 14 4 4M9 11h4m-2-2v4" /></svg>
        </button> : null}
      </span>
      {capability?.can_zoom ? movementButton("zoom", "in", "+", t("ptz.zoomIn")) : <span />}
    </div> : null}
    {capability?.can_pan_tilt || capability?.can_zoom ? <label className="ptzSpeed"><span>{t("ptz.speed")}</span>
      <span className="ptzSpeedTrack"><input type="range" min="1" max="5" step="1" value={speed} aria-label={t("ptz.speed")}
        onPointerDown={event => {
          if (event.button !== 0) return;
          // Cancel native HTML card dragging, not the range's keyboard semantics.
          event.preventDefault(); event.stopPropagation(); event.currentTarget.focus();
          speedPointer.current = event.pointerId; event.currentTarget.setPointerCapture?.(event.pointerId);
          const rect = event.currentTarget.getBoundingClientRect(); setSpeed(ptzSpeedLevel(event.clientX, rect.left, rect.width));
        }}
        onPointerMove={event => {
          if (speedPointer.current !== event.pointerId) return;
          event.preventDefault(); event.stopPropagation();
          const rect = event.currentTarget.getBoundingClientRect(); setSpeed(ptzSpeedLevel(event.clientX, rect.left, rect.width));
        }}
        onPointerUp={event => { event.stopPropagation(); speedPointer.current = null; }}
        onPointerCancel={() => { speedPointer.current = null; }} onLostPointerCapture={() => { speedPointer.current = null; }}
        onChange={event => setSpeed(Number(event.target.value))} />
        <span className="ptzSpeedTicks" aria-hidden="true">{[1,2,3,4,5].map(level => <i key={level} />)}</span>
      </span></label> : null}
    {showPresets ? <div className="ptzSavedPosition">
      <select value={selected} aria-label={t("ptz.savedPosition")} disabled={presetsState !== "loaded" || !saved.length || presetBusy}
        aria-busy={presetBusy} onChange={event => { void go(event.target.value); }}>
        <option value="">{presetsState === "loaded" && !saved.length ? t("ptz.noPositions") : t("ptz.savedPosition")}</option>
        {saved.map((preset, index) => <option key={preset.token} value={preset.token}>{preset.name || `${t("ptz.position")} ${index + 1}`}</option>)}
      </select>
    </div> : null}
    <PtzToast toast={toast} onClose={() => setToast(null)} />
  </section>;
}

export function CameraPtzDialog({ camera, capability, onClose }) {
  const { t } = useI18n();
  const [pointActive, setPointActive] = useState(false);
  const [areaZoomActive, setAreaZoomActive] = useState(false);
  const [toast, setToast] = useState(null);
  const { controller } = usePtzController(camera?.id, capability?.profile_token, () => setToast({
    id: `ptz-point-${Date.now()}`, title: t("ptz.title"), message: t("ptz.commandFailed"), tone: "error",
  }));
  if (!camera) return null;
  return <><OperationDialog dialog={{ id: `camera-ptz-${camera.id}`, title: camera.name,
    className: "cameraPtzDialog", tone: "info", closeLabel: t("common.close"), showFooterClose: false,
    content: <div className="cameraPtzDialogBody">
      <div className="cameraPtzPreview"><TilePlayer cameraId={camera.id} stream={camera.rtsp_sub_url ? "sub" : "main"}
        lowLatency pointActive={pointActive} onPoint={point => controller.point(point)}
        areaZoomActive={areaZoomActive} onAreaZoom={rectangle => controller.areaZoom(rectangle)}
        onPtzAimCancel={() => controller.stop()}
        onLowLatencyUnavailable={() => { setPointActive(false); setAreaZoomActive(false); setToast({id:`ptz-video-${Date.now()}`,
          title:t("ptz.title"),message:t("ptz.lowLatencyUnavailable"),tone:"warning"}); }} /></div>
      <CameraPtzControls camera={camera} capability={capability} pointActive={pointActive}
        onPointModeChange={active => { setPointActive(active); setAreaZoomActive(false); }}
        areaZoomActive={areaZoomActive} onAreaZoomModeChange={active => { setAreaZoomActive(active); setPointActive(false); }} />
    </div>,
  }} onClose={onClose} /><PtzToast toast={toast} onClose={() => setToast(null)} /></>;
}
