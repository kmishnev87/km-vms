"use client";

import { Children, cloneElement, isValidElement, useEffect, useId, useRef, useState } from "react";
import AuthenticatedPreviewImage from "../AuthenticatedPreviewImage";
import { OperationDialog, useModalBodyScrollLock } from "../OperationFeedback";
import { CAMERA_MANAGEMENT_FIELDS, cameraAssignedProfile, cameraCheckIsCurrent, cameraManagementKey, cameraProfileGroups, cameraProfileHasMetadata, cameraProfilePath, cameraSelectedSource, cameraSourceConflict, cameraStreamKey, cameraStreamRoles } from "../../lib/cameraEditor";
import { useLocaleText } from "../../lib/i18n";
import CameraDeviceSettings from "./CameraDeviceSettings";
import useCameraEditor from "./useCameraEditor";

function StreamChoice({ label, value, disabled, onChange, name, metadata, children }) {
  const id = useId();
  return <div className="cameraFlowStreamSelect">
    <select className="select" value={value} disabled={disabled} onChange={onChange} aria-label={label} aria-describedby={`${id}-metadata`}>{children}</select>
    <span className="cameraFlowStreamSelected" aria-hidden="true"><span>{name}</span><span>{metadata}</span></span>
    <span className="cameraFlowStreamDescription" id={`${id}-metadata`}>{metadata}</span>
  </div>;
}

function Field({ label, hint, children, wide = false }) {
  const id = useId();
  return <label className={wide ? "cameraFlowField cameraFlowWide" : "cameraFlowField"}>
    <span id={`${id}-label`}>{label}</span>{Children.map(children, (child) => isValidElement(child) && ["input", "select"].includes(child.type)
      ? cloneElement(child, { "aria-labelledby": `${id}-label`, "aria-describedby": hint ? `${id}-hint` : undefined }) : child)}
    {hint ? <span id={`${id}-hint`} className="cameraFlowFieldNote">{hint}</span> : null}
  </label>;
}

function streamDescription(profile, result, copy) {
  const measured = result?.video || profile?.rtsp_probe?.video;
  const video = measured || profile?.video;
  const current = measured ? null : profile?.current || profile?.settings;
  const rawFps = current?.fps || video?.fps;
  const fraction = String(rawFps || "").match(/^(\d+)\/(\d+)$/);
  const fps = fraction && Number(fraction[2]) ? Math.round(Number(fraction[1]) / Number(fraction[2]) * 100) / 100 : rawFps;
  const parts = [current?.codec || video?.codec,
    current?.resolution || (video?.width && video?.height ? `${video.width} × ${video.height}` : ""),
    fps ? `${fps} FPS` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : copy.videoParametersUnavailable;
}

export default function CameraEditor({ camera = null, onClose, onSaved }) {
  const copy = useLocaleText("cameras");
  const editor = useCameraEditor(camera, copy, onSaved);
  const { form, patch, section, setSection, device, preview, operation, error, notice } = editor;
  const childReturnFocusRef = useRef(null);
  const childWasOpenRef = useRef(false);
  const [confirm, setConfirm] = useState(null);
  const [discoveryOpen, setDiscoveryOpen] = useState(false);
  useEffect(() => { if (editor.discovery) setDiscoveryOpen(true); }, [editor.discovery]);
  const [deviceBusy, setDeviceBusy] = useState(false);
  const dialogRef = useRef(null);
  const pendingAction = useRef(null);
  const busy = Boolean(operation) || deviceBusy;
  const busyRef = useRef(busy);
  const closeRef = useRef(null);
  const bodyRef = useRef(null);
  const imageCache = useRef(new Map());
  useModalBodyScrollLock(true);

  useEffect(() => {
    const childOpen = Boolean(discoveryOpen || confirm);
    if (childWasOpenRef.current && !childOpen) {
      const trigger = childReturnFocusRef.current;
      const target = trigger?.isConnected && !trigger.disabled ? trigger : dialogRef.current?.querySelector("button:not([disabled]), input:not([disabled])");
      target?.focus({ preventScroll: true });
      childReturnFocusRef.current = null;
    }
    childWasOpenRef.current = childOpen;
  }, [discoveryOpen, confirm]);

  function requestClose() {
    if (operation === "saving" || deviceBusy) return;
    if (editor.dirty) {
      childReturnFocusRef.current = document.activeElement;
      pendingAction.current = onClose;
      setConfirm("close");
    } else onClose();
  }
  closeRef.current = requestClose;
  busyRef.current = operation === "saving" || deviceBusy;

  useEffect(() => {
    const previousFocus = document.activeElement;
    dialogRef.current?.querySelector("input, button")?.focus({ preventScroll: true });
    return () => { if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true }); };
  }, []);
  useEffect(() => () => {
    for (const url of imageCache.current.values()) URL.revokeObjectURL(url);
    imageCache.current.clear();
  }, []);
  useEffect(() => { bodyRef.current?.scrollTo({ top: 0 }); }, [section]);
  useEffect(() => {
    if (!editor.dirty) return;
    const handler = (event) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [editor.dirty]);

  function guardDeviceDraft(action) {
    if (deviceBusy) return;
    if (editor.settingsDirty) { childReturnFocusRef.current = document.activeElement; pendingAction.current = action; setConfirm("device"); }
    else action();
  }
  function change(key, value) {
    if (CAMERA_MANAGEMENT_FIELDS.has(key)) guardDeviceDraft(() => patch(key, value));
    else patch(key, value);
  }
  function navigate(nextSection) {
    if (busy || nextSection === section) return;
    guardDeviceDraft(async () => {
      editor.setError("");
      if (nextSection === "video") {
        if (camera && !device && !await editor.check({ all: true, automatic: true, image: !preview })) return;
        if (!await editor.prepareProfileMetadata()) return;
      }
      setSection(nextSection);
    });
  }
  function handleKeyDown(event) {
    if (event.defaultPrevented || event.target.closest(".operationFeedbackDialog")) return;
    if (event.key === "Escape") {
      event.preventDefault(); event.stopPropagation();
      if (!busyRef.current) closeRef.current?.();
    }
    if (event.key !== "Tab") return;
    const elements = [...dialogRef.current.querySelectorAll("button:not([disabled]), input:not([disabled]), select:not([disabled]), summary, [tabindex='0']")].filter((element) => element.getClientRects().length);
    const first = elements[0], last = elements[elements.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }

  const profiles = device?.profiles || [];
  const sourceGroups = cameraProfileGroups(device);
  const selectedSource = cameraSelectedSource(form, device);
  const sourceProfiles = selectedSource ? profiles.filter((profile) => selectedSource.profile_tokens.includes(profile.token)) : profiles;
  const roles = cameraStreamRoles(form);
  const assigned = (role) => cameraAssignedProfile(form, device, role);
  const roleLabel = (role) => role === "sub" ? copy.editorSub : copy.editorMain;
  const currentResult = (role) => cameraCheckIsCurrent(editor.checks[role], cameraStreamKey(form, role)) ? editor.checks[role].result : null;
  const choices = [{ id: "connection", label: copy.editorConnection }, { id: "video", label: copy.editorVideoRecording }, { id: "review", label: copy.editorReview }];
  const currentIndex = choices.findIndex((item) => item.id === section);
  const input = (key, label, options = {}) => <Field key={key} label={label} hint={options.hint} wide={options.wide}>
    <input className="input" type={options.type || "text"} value={form[key]} autoComplete={options.autoComplete || "off"}
      min={options.min} max={options.max} disabled={operation === "saving" || deviceBusy} onChange={(event) => change(key, event.target.value)} />
  </Field>;
  const profileName = (profile, index) => {
    return profile.stream_kind === "main" ? copy.editorMain : profile.stream_kind === "sub"
      ? `${copy.editorSub}${profile.stream_index ? ` ${profile.stream_index}` : ""}` : `${copy.editorStreamVariant} ${index + 1}`;
  };
  const profileMetadata = (profile, result = null) => result?.video || profile?.rtsp_probe || cameraProfileHasMetadata(profile) ? streamDescription(profile, result, copy)
    : profile?.metadata_status?.state === "failed" || (profile && !cameraProfilePath(profile)) ? copy.editorMetadataUnavailable
    : profile ? copy.editorMetadataLoading : streamDescription(profile, result, copy);
  const profileLabel = (profile, index) => `${profileName(profile, index)} — ${profileMetadata(profile)}`;

  function renderSourceChoice() {
    if (sourceGroups.length < 2) return null;
    return <div className="cameraFlowSourceChoice"><Field label={copy.editorSource} hint={copy.editorSourceHelp}>
      <select className="select" value={selectedSource?.id || ""} disabled={operation === "saving" || deviceBusy} onChange={(event) => guardDeviceDraft(() => editor.selectSource(event.target.value))}>
        {!selectedSource ? <option value="">{copy.editorChooseSource}</option> : null}
        {sourceGroups.map((group, index) => <option key={group.id} value={group.id}>{group.id === "unknown" ? copy.editorUnknownSource : `${copy.editorSource} ${group.channel ?? index + 1}`}</option>)}
      </select>
    </Field></div>;
  }

  function renderUnverifiedChoice() {
    if (editor.verified || (camera && !editor.connectionChanged)) return null;
    return <label className="cameraFlowCheckbox"><input type="checkbox" checked={form.manual_confirm_unverified} disabled={busy} onChange={(event) => patch("manual_confirm_unverified", event.target.checked)} />{copy.saveUnverified}</label>;
  }

  function renderRecording() {
    return <section className="cameraFlowRecordSection">
      <h3>{copy.editorRecordingRules}</h3><p className="cameraFlowHint">{copy.editorRecordingHelp}</p>
      <div className="cameraFlowFields">
        <Field label={copy.editorRecordingEnabled}>
          <select className="select" value={form.enabled ? "on" : "off"} onChange={(event) => patch("enabled", event.target.value === "on")} disabled={busy}>
            <option value="on">{copy.editorRecordOn}</option><option value="off">{copy.editorRecordOff}</option>
          </select>
        </Field>
        <Field label={copy.recordingMode}>
          <select className="select" value={form.recording_mode} onChange={(event) => patch("recording_mode", event.target.value)} disabled={busy}>
            <option value={form.recording_mode}>{form.recording_mode === "always" ? copy.always : form.recording_mode}</option>
          </select>
        </Field>
        {input("segment_minutes", copy.segmentDuration, { type: "number", min: 1, hint: copy.minutesShort })}
        {input("retention_days", copy.retentionDays, { type: "number", min: 1, hint: copy.daysShort })}
        {input("storage_quota_gb", copy.archiveLimitGb, { type: "number", min: 1 })}
      </div>
    </section>;
  }

  function renderPurposeChoice(purpose, label) {
    const role = form[purpose === "record" ? "default_record_stream" : "default_live_stream"];
    const selected = assigned(role);
    const token = selected?.token || form[role === "main" ? "onvif_profile_token" : "onvif_sub_profile_token"] || "";
    const streamError = editor.streamErrors[role];
    const failure = streamError?.key === cameraStreamKey(form, role) ? streamError.message : "";
    const onvifChoices = form.protocol === "onvif" && sourceProfiles.length > 0;
    return <Field label={label} key={purpose}>
      <StreamChoice label={label} value={onvifChoices ? token : role} disabled={operation === "saving" || deviceBusy}
        name={onvifChoices && selected ? profileName(selected, sourceProfiles.indexOf(selected)) : roleLabel(role)} metadata={profileMetadata(selected, currentResult(role))}
        onChange={(event) => onvifChoices ? guardDeviceDraft(() => editor.selectPurpose(purpose, event.target.value))
          : patch(purpose === "record" ? "default_record_stream" : "default_live_stream", event.target.value)}>
        {onvifChoices ? <>
          {!sourceProfiles.some((profile) => profile.token === token) ? <option value={token}>{copy.editorUnlistedStream}</option> : null}
          {sourceProfiles.map((profile, index) => <option key={profile.token} value={profile.token}>{profileLabel(profile, index)}</option>)}
        </> : roles.map((item) => <option key={item} value={item}>{roleLabel(item)}</option>)}
      </StreamChoice>
      <span className={`cameraFlowStreamStatus${failure ? " failed" : ""}`} aria-live="polite">
        {failure || (currentResult(role) ? copy.editorStreamVerified : operation === "video" ? copy.editorStreamChecking : copy.editorStreamNotChecked)}
      </span>
    </Field>;
  }

  function renderConnection() {
    return <section>
      <div className="cameraFlowSectionHead"><h3>{copy.editorConnectCamera}</h3>
        {form.protocol === "onvif" ? <button type="button" className="cameraFlowTextButton" onClick={(event) => { childReturnFocusRef.current = event.currentTarget; editor.discover(); }} disabled={editor.searching || busy}>{editor.searching ? copy.searching : copy.editorSearch}</button> : null}
      </div>
      {!camera ? <div className="cameraFlowMethods" aria-label={copy.protocol}>
        {[["onvif", copy.editorOnvifHelp], ["rtsp", copy.editorRtspHelp]].map(([protocol, description]) => <button key={protocol} type="button" aria-pressed={form.protocol === protocol} disabled={busy} onClick={() => change("protocol", protocol)}>
          <strong>{protocol.toUpperCase()}</strong><span>{description}</span>
        </button>)}
      </div> : null}
      <div className="cameraFlowFields">
        {input("name", copy.cameraName, { wide: true })}
        {input("host", form.protocol === "onvif" ? copy.onvifHostIp : copy.ipHost)}
        {input("port", form.protocol === "onvif" ? copy.onvifPort : copy.rtspPort, { type: "number", min: 1, max: 65535 })}
        {input("username", copy.login, { autoComplete: "username" })}
        {input("password", copy.password, { type: "password", autoComplete: "new-password", hint: camera ? copy.passwordEditHint : "" })}
        {form.protocol === "rtsp" ? <>
          {input("rtsp_main_url", copy.rtspMainPathUrl, { wide: true })}
          {input("rtsp_sub_url", copy.rtspSubPathUrl, { wide: true, hint: copy.editorOptional })}
        </> : null}
      </div>
      <details className="cameraFlowAdvanced">
        <summary>{copy.editorAdvancedConnection}</summary>
        <div className="cameraFlowFields">
          {camera ? <Field label={copy.protocol}><select className="select" value={form.protocol} disabled={busy} onChange={(event) => change("protocol", event.target.value)}><option value="onvif">ONVIF</option><option value="rtsp">RTSP</option></select></Field> : null}
          {form.protocol === "onvif" ? <>
            {input("rtsp_host", copy.rtspReachableHost)}{input("rtsp_port", copy.rtspReachablePort, { type: "number", min: 1, max: 65535 })}
            {input("onvif_path", copy.onvifPath)}{input("onvif_channel_id", copy.channelId)}
          </> : null}
          <Field label={copy.rtspTransport}><select className="select" value={form.rtsp_transport} disabled={busy} onChange={(event) => patch("rtsp_transport", event.target.value)}><option value="tcp">TCP</option><option value="udp">UDP</option></select></Field>
        </div>
        {renderUnverifiedChoice()}
      </details>
      <p className="cameraFlowHint">{copy.editorDirectAddressHelp}</p>
      {renderSourceChoice()}
      {camera ? <button type="button" className="button secondary small" onClick={() => editor.check({ force: true })} disabled={busy}>{operation === "connection" ? copy.checking : copy.editorCheckConnection}</button> : null}
    </section>;
  }

  function renderVideo() {
    return <section>
      <div className="cameraFlowSectionHead"><h3>{copy.editorVideoSources}</h3>
        <button type="button" className="cameraFlowTextButton" disabled={busy} onClick={() => editor.check({ all: true, force: true })}>{copy.editorRefreshVideo}</button>
      </div>
      <p className="cameraFlowHint">{copy.editorStreamHelp}</p>
      <div className="cameraFlowSourceStatus" role="status">{cameraSourceConflict(form, device) ? copy.editorSourceConflict
        : selectedSource?.recommendation_basis === "unavailable" ? copy.editorRecommendationUnknown
        : selectedSource?.id === "unknown" && profiles.length > 1 ? copy.editorUnknownSourceHelp : roles.length === 1 ? copy.editorSingleStream : ""}</div>
      <div className="cameraFlowStreamChoices">
        {renderPurposeChoice("record", copy.recordStream)}{renderPurposeChoice("live", copy.liveStream)}
      </div>
      <details className="cameraFlowAdvanced"><summary>{copy.editorStreamPaths}</summary><div className="cameraFlowFields">
        {input("rtsp_main_url", copy.rtspMainPathUrl, { wide: true })}{input("rtsp_sub_url", copy.rtspSubPathUrl, { wide: true })}
        {form.protocol === "onvif" ? <>{input("onvif_profile_token", copy.profileToken)}{input("onvif_sub_profile_token", `${copy.editorSub} — ${copy.profileToken}`)}</> : null}
      </div>{renderUnverifiedChoice()}</details>
      {form.protocol === "onvif" ? <CameraDeviceSettings key={`${cameraManagementKey(form)}:${selectedSource?.id || ""}`} cameraId={camera?.id} form={form} profiles={sourceProfiles}
        copy={copy} onDirtyChange={editor.setSettingsDirty} onBusyChange={setDeviceBusy} onVideoChanged={editor.invalidateVideo} onSnapshot={editor.refreshProfile} /> : <p className="cameraFlowHint">{copy.editorRtspDeviceHelp}</p>}
    </section>;
  }

  function renderReview() {
    const row = (label, value) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>;
    return <section>
      <h3>{copy.editorReviewTitle}</h3><p className="cameraFlowHint">{copy.editorReviewHelp}</p>
      <div className="cameraFlowReviewGroup"><div className="cameraFlowSectionHead"><h4>{copy.editorConnection}</h4><button type="button" className="cameraFlowTextButton" onClick={() => navigate("connection")}>{copy.edit}</button></div>
        <dl>{row(copy.cameraName, form.name)}{row(copy.address, `${form.host}:${form.port}`)}{row(copy.protocol, form.protocol.toUpperCase())}</dl>
      </div>
      <div className="cameraFlowReviewGroup"><div className="cameraFlowSectionHead"><h4>{copy.editorVideoRecording}</h4><button type="button" className="cameraFlowTextButton" onClick={() => navigate("video")}>{copy.edit}</button></div>
        <dl>{sourceGroups.length > 1 ? row(copy.editorSource, `${copy.editorSource} ${selectedSource?.channel ?? sourceGroups.indexOf(selectedSource) + 1}`) : null}
          {row(copy.recordStream, `${roleLabel(form.default_record_stream)} — ${streamDescription(assigned(form.default_record_stream), currentResult(form.default_record_stream), copy)}`)}
          {row(copy.liveStream, `${roleLabel(form.default_live_stream)} — ${streamDescription(assigned(form.default_live_stream), currentResult(form.default_live_stream), copy)}`)}{row(copy.recording, form.enabled ? copy.editorRecordOn : copy.editorRecordOff)}
          {row(copy.segmentDuration, `${form.segment_minutes} ${copy.minutesShort}`)}{row(copy.retentionDays, `${form.retention_days} ${copy.daysShort}`)}{row(copy.limit, `${form.storage_quota_gb} ${copy.gbShort}`)}</dl>
      </div>
      <p className="cameraFlowReviewStatus">{editor.verified ? copy.editorVideoChecked : form.manual_confirm_unverified ? copy.editorUnverifiedNotice : copy.editorRecheck}</p>
    </section>;
  }

  const primaryAction = camera || section === "review" ? editor.save
    : section === "connection" && !editor.connectionReady && !form.manual_confirm_unverified ? () => editor.check() : editor.next;
  const preparing = editor.metadataPreparing && section === "connection" && editor.connectionReady && !form.manual_confirm_unverified;
  const primaryLabel = operation === "saving" ? copy.saving : busy ? copy.checking : preparing ? copy.editorPreparingStreams : camera ? copy.editorSaveChanges
    : section === "review" ? copy.editorAddCamera : section === "connection"
      ? editor.connectionReady || form.manual_confirm_unverified ? copy.editorContinue : copy.editorCheckConnection : copy.editorContinue;
  const feedback = error || (busy ? operation === "preview" ? copy.editorLoadingImage : copy.checking : preparing ? copy.editorPreparingStreams
    : section === "review" ? form.manual_confirm_unverified ? copy.editorUnverifiedNotice : "" : notice);

  return <div className="cameraFlowBackdrop" role="presentation">
    <div className="cameraFlow" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="camera-flow-title" aria-busy={busy} aria-hidden={discoveryOpen || confirm ? true : undefined} inert={discoveryOpen || confirm ? true : undefined} onKeyDown={handleKeyDown}>
      <header className="cameraFlowHeader"><div><h2 id="camera-flow-title">{camera ? `${copy.editTitle}: ${camera.name}` : copy.addTitle}</h2>
        <p>{camera ? copy.editorEditSubtitle : copy.editorAddSubtitle}</p></div>
        <button type="button" className="cameraFlowClose" onClick={requestClose} disabled={operation === "saving" || deviceBusy} aria-label={copy.close}>×</button>
      </header>
      {camera ? <div className="cameraFlowTabs" role="tablist" aria-label={copy.editTitle}>
        {[["connection", copy.editorConnection], ["video", copy.video], ["recording", copy.recording]].map(([id, label]) => <button type="button" key={id} role="tab" id={`camera-tab-${id}`} aria-controls="camera-flow-panel" aria-selected={section === id} disabled={busy} onClick={() => navigate(id)}>{label}</button>)}
      </div> : <nav className="cameraFlowSteps" aria-label={copy.editorSteps}>
        {choices.map((choice, index) => <button type="button" key={choice.id} aria-current={choice.id === section ? "step" : undefined} disabled={busy || index > currentIndex} onClick={() => navigate(choice.id)}>
          <span>{index + 1}</span>{choice.label}
        </button>)}
      </nav>}
      <div className="cameraFlowBody" ref={bodyRef}>
        <div className="cameraFlowMain" id="camera-flow-panel" role={camera ? "tabpanel" : undefined} aria-labelledby={camera ? `camera-tab-${section}` : undefined}>
          <fieldset className="cameraFlowFieldset" disabled={operation === "saving" || deviceBusy}>
            {section === "connection" ? renderConnection() : section === "review" ? renderReview() : <>{section === "video" ? renderVideo() : null}{!camera || section === "recording" ? renderRecording() : null}</>}
          </fieldset>
        </div>
        <aside className="cameraFlowPreview" aria-label={copy.cameraFrame}>
          <div className="cameraFlowPreviewHead"><strong>{copy.editorPreview}</strong><span>{sourceGroups.length > 1
            ? selectedSource ? `${copy.editorSource} ${selectedSource.channel ?? sourceGroups.indexOf(selectedSource) + 1}` : copy.editorUnknownSource : copy.editorIdentificationFrame}</span></div>
          <div className="cameraFlowFrame">
            <AuthenticatedPreviewImage key={preview?.validation_token || `saved-${camera?.id}`} cache={imageCache} src={preview?.preview_url || (!editor.connectionChanged ? camera?.preview_url : null)} alt={form.name}
              fallback={<div className="cameraFlowEmptyFrame"><strong>{preview ? copy.frameUnavailable : copy.editorWaitingFrame}</strong><span>{preview ? copy.editorFrameMissing : copy.editorFrameHelp}</span></div>} />
            {operation === "preview" || operation === "connection" ? <span className="cameraFlowFrameLoading" role="status">{copy.editorLoadingImage}</span> : null}
          </div>
          <div className="cameraFlowPreviewInfo"><strong>{device?.device?.model || form.name || copy.camera}</strong><span>{device?.device?.manufacturer || `${form.host}${form.host ? `:${form.port}` : ""}`}</span>
            <p>{preview ? copy.editorIdentificationHelp : camera ? copy.editorSavedNotChecked : copy.editorImageCheckHint}</p>
          </div>
        </aside>
      </div>
      <div className={`cameraFlowFeedback${error ? " failed" : ""}`} role={error ? "alert" : "status"} aria-live="polite">{feedback || "\u00a0"}</div>
      <footer className="cameraFlowFooter"><span>{camera ? copy.editorChangesLocal : copy.editorNotSavedYet}</span><div>
        <button type="button" className="cameraFlowTextButton" disabled={operation === "saving" || deviceBusy} onClick={requestClose}>{copy.cancel}</button>
        {!camera && section !== "connection" ? <button type="button" className="button secondary" disabled={busy} onClick={() => navigate(section === "review" ? "video" : "connection")}>{copy.editorBack}</button> : null}
        <button type="button" className="button" disabled={busy || preparing} onClick={primaryAction}>{primaryLabel}</button>
      </div></footer>
    </div>
    <OperationDialog dialog={discoveryOpen ? {
      id: "camera-discovery", title: copy.editorSearch, closeLabel: copy.close, tone: "neutral",
      content: <div className="cameraFlowDiscovery"><p>{editor.discovery?.candidates?.length ? copy.editorChooseDiscovered : copy.editorDiscoveryEmpty}</p>
        {(editor.discovery?.candidates || []).map((candidate) => <button type="button" key={candidate.id || `${candidate.host}:${candidate.port}`} disabled={busy} onClick={() => { setDiscoveryOpen(false); guardDeviceDraft(() => editor.selectCandidate(candidate)); }}>{candidate.host}:{candidate.port || 80}</button>)}
      </div>,
    } : null} onClose={() => setDiscoveryOpen(false)} />
    <OperationDialog dialog={confirm ? {
      id: "camera-editor-discard", presentation: "compact-confirmation", tone: "warning",
      title: confirm === "device" ? copy.editorDeviceUnsaved : copy.editorUnsavedTitle,
      message: confirm === "device" ? copy.editorDeviceDiscardQuestion : copy.editorUnsavedQuestion,
      confirmLabel: confirm === "device" ? copy.editorDiscardContinue : copy.editorDiscardClose, cancelLabel: copy.editorKeepEditing, closeLabel: copy.close,
      onConfirm: () => { setConfirm(null); editor.setSettingsDirty(false); pendingAction.current?.(); pendingAction.current = null; },
    } : null} onClose={() => { setConfirm(null); pendingAction.current = null; }} />
  </div>;
}
