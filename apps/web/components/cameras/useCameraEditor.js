"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../../lib/api";
import { onvifConfigFromResult } from "../../lib/cameraOnvifConfig";
import {
  CAMERA_CONNECTION_FIELDS, CAMERA_MANAGEMENT_FIELDS, assignCameraProfile, assignCameraPurposeProfile, cameraPreviewIdentity,
  cameraProfileHasMetadata, cameraProfilePath, cameraSelectedSource,
  cameraCheckIsCurrent, cameraChecksReady, cameraConnectionChanged, cameraDraftWithProfiles,
  cameraEditorChanges, cameraEditorDraft, cameraEditorError, cameraEditorPayload,
  cameraEditorProblem, cameraManagementKey, cameraSavePayload, cameraStreamKey, cameraDraftForSource, cameraSourceConflict,
  cameraStreamRoles, patchCameraEditor,
} from "../../lib/cameraEditor";

async function post(path, payload) {
  return apiFetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
}

function discardFrames(tokens) {
  for (let index = 0; index < tokens.length; index += 128) {
    apiFetch("/previews/camera-tests/cleanup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tokens: tokens.slice(index, index + 128) }), keepalive: true,
    }).catch(() => {});
  }
}

export default function useCameraEditor(camera, copy, onSaved) {
  const baseline = useRef(cameraEditorDraft(camera));
  const [form, setForm] = useState(baseline.current);
  const formRef = useRef(form);
  const [section, setSection] = useState(camera ? "recording" : "connection");
  const [checks, setChecks] = useState({});
  const checksRef = useRef({});
  const [device, setDeviceState] = useState(null);
  const deviceRef = useRef(null);
  const [discovery, setDiscovery] = useState(null);
  const [preview, setPreview] = useState(null);
  const previewRef = useRef(null);
  const previewCache = useRef(new Map());
  const checkCache = useRef(new Map());
  const queuedCheck = useRef(null);
  const [streamErrors, setStreamErrors] = useState({});
  const [operation, setOperation] = useState("");
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [deviceVideoChanged, setDeviceVideoChanged] = useState(false);
  const alive = useRef(true);
  const revision = useRef(0);
  const checking = useRef(false);
  const discoveryRequest = useRef(0);
  const temporaryFrames = useRef(new Set());
  const metadataRequested = useRef(false);
  const metadataRun = useRef(null);
  const metadataActive = useRef(null);
  const metadataScope = useRef(null);
  const metadataGeneration = useRef(0);
  const [metadataPreparing, setMetadataPreparing] = useState(false);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false; revision.current += 1; discoveryRequest.current += 1;
      queuedCheck.current?.resolve(false); queuedCheck.current = null;
      discardFrames([...temporaryFrames.current]); temporaryFrames.current.clear();
    };
  }, []);

  function replaceForm(next) {
    formRef.current = next;
    setForm(next);
    return next;
  }
  function rememberCheck(role, check) {
    checksRef.current = { ...checksRef.current, [role]: check };
    setChecks(checksRef.current);
  }
  function setDevice(next) {
    const value = typeof next === "function" ? next(deviceRef.current) : next;
    deviceRef.current = value;
    setDeviceState(value);
  }
  function showPreview(value) {
    previewRef.current = value;
    setPreview(value);
  }
  function restoreChecks(next) {
    const restored = { management: checksRef.current.management };
    for (const role of cameraStreamRoles(next)) {
      const key = cameraStreamKey(next, role);
      const cached = checkCache.current.get(key);
      if (cameraCheckIsCurrent(cached, key)) restored[role] = cached;
    }
    checksRef.current = restored; setChecks(restored);
    return { ...next, main_validation_token: restored.main?.token || null,
      sub_validation_token: restored.sub?.token || null, validation_token: restored.main?.token || null,
      preview_token: previewRef.current?.preview_token || null };
  }
  function collectUnusedFrames() {
    while (checkCache.current.size > 24) checkCache.current.delete(checkCache.current.keys().next().value);
    while (previewCache.current.size > 8) previewCache.current.delete(previewCache.current.keys().next().value);
    const retained = new Set([previewRef.current?.preview_token,
      ...[...checkCache.current.values()].map((item) => item.result?.preview_token),
      ...[...previewCache.current.values()].map((item) => item.preview_token)]);
    const unused = [...temporaryFrames.current].filter((token) => !retained.has(token));
    if (unused.length) discardFrames(unused);
    for (const token of unused) temporaryFrames.current.delete(token);
  }
  function patch(key, value) {
    const previous = formRef.current;
    if (previous[key] === value) return;
    if (CAMERA_CONNECTION_FIELDS.has(key)) {
      revision.current += 1;
      setError(""); setNotice("");
      if (CAMERA_MANAGEMENT_FIELDS.has(key)) { setDevice(null); setSettingsDirty(false); showPreview(null); }
      if (key === "onvif_channel_id") showPreview(null);
      if (CAMERA_MANAGEMENT_FIELDS.has(key) || ["rtsp_transport", "onvif_channel_id"].includes(key)) invalidateMetadata();
    }
    replaceForm(restoreChecks(patchCameraEditor(previous, key, value)));
  }
  function selectProfile(role, token) {
    const profile = deviceRef.current?.profiles?.find((item) => item.token === token);
    revision.current += 1;
    replaceForm(restoreChecks(assignCameraProfile(formRef.current, role, profile)));
    setError(""); setNotice("");
    return check({ all: true, automatic: true, image: false });
  }
  function selectPurpose(purpose, token) {
    revision.current += 1;
    replaceForm(restoreChecks(assignCameraPurposeProfile(formRef.current, deviceRef.current, purpose, token)));
    setError(""); setNotice("");
    return check({ all: true, automatic: true, image: false });
  }
  function selectSource(sourceId) {
    revision.current += 1;
    const next = cameraDraftForSource(formRef.current, deviceRef.current, sourceId);
    showPreview(previewCache.current.get(cameraPreviewIdentity(next, deviceRef.current)) || null);
    // A -> B -> A is a NEW read, even if the original A request is still in flight.
    checkCache.current.clear();
    replaceForm(restoreChecks(next));
    startMetadataScope({ restart: true, refresh: true });
    setError(""); setNotice("");
    return check({ automatic: true, image: true, force: true });
  }
  function selectCandidate(candidate) {
    revision.current += 1;
    let next = formRef.current;
    for (const [key, value] of Object.entries({
      protocol: "onvif", host: candidate.host, port: candidate.port || 80,
      rtsp_host: candidate.host, rtsp_port: 554, onvif_path: candidate.xaddr_path || "",
      rtsp_main_url: "", rtsp_sub_url: "", onvif_profile_token: "", onvif_sub_profile_token: "",
    })) next = patchCameraEditor(next, key, value);
    replaceForm(next);
    setDevice(null); showPreview(null); setSettingsDirty(false); setError(""); setNotice(copy.editorCandidateSelected);
    invalidateMetadata();
  }

  async function discover() {
    const request = ++discoveryRequest.current;
    setSearching(true); setError("");
    try {
      const result = await post("/cameras/onvif/discover", { timeout_seconds: 5 });
      if (alive.current && request === discoveryRequest.current) setDiscovery(result);
    } catch (err) {
      if (alive.current && request === discoveryRequest.current) setError(cameraEditorError(err, copy));
    } finally {
      if (alive.current && request === discoveryRequest.current) setSearching(false);
    }
  }

  function stillCurrent(request) { return alive.current && revision.current === request; }

  function metadataKey() {
    return JSON.stringify([cameraManagementKey(formRef.current), formRef.current.rtsp_transport,
      cameraSelectedSource(formRef.current, deviceRef.current)?.id || formRef.current.onvif_channel_id || "single"]);
  }
  function metadataProfiles() {
    const data = deviceRef.current;
    const source = cameraSelectedSource(formRef.current, data);
    return (data?.profiles || []).filter((profile) => source ? source.profile_tokens.includes(profile.token) : !(data?.source_groups?.length > 1));
  }
  function metadataIsCurrent(scope) {
    return alive.current && scope && scope === metadataScope.current && scope.key === metadataKey();
  }
  function pendingMetadataProfile(scope = metadataScope.current) {
    if (!metadataIsCurrent(scope)) return null;
    return metadataProfiles().find((profile) => cameraProfilePath(profile)
      && !(profile.metadata_status?.generation === scope.generation && ["done", "failed"].includes(profile.metadata_status.state))
      && (scope.refresh || (!cameraProfileHasMetadata(profile) && !profile.rtsp_probe)));
  }
  function invalidateMetadata() {
    metadataScope.current = null;
    metadataRequested.current = false;
    setMetadataPreparing(false);
  }
  function startMetadataScope({ restart = false, refresh = false } = {}) {
    if (formRef.current.protocol !== "onvif" || !deviceRef.current) return null;
    const key = metadataKey();
    if (restart || metadataScope.current?.key !== key) {
      metadataScope.current = { key, generation: ++metadataGeneration.current, refresh };
      if (refresh) {
        const tokens = new Set(metadataProfiles().map((profile) => profile.token));
        setDevice((previous) => ({ ...previous, profiles: previous.profiles.map((profile) => {
          if (!tokens.has(profile.token)) return profile;
          const { rtsp_probe, metadata_status, ...base } = profile;
          return base;
        }) }));
      }
    }
    metadataRequested.current = true;
    setMetadataPreparing(Boolean(pendingMetadataProfile()));
    return metadataScope.current;
  }
  function updateProfileMetadata(token, scope, changes) {
    if (!metadataIsCurrent(scope)) return;
    setDevice((previous) => previous ? { ...previous, profiles: (previous.profiles || []).map((profile) => (
      profile.token === token ? { ...profile, ...changes } : profile
    )) } : previous);
    setMetadataPreparing(Boolean(pendingMetadataProfile(scope)));
  }
  async function loadProfileMetadata() {
    if (metadataRun.current) return metadataRun.current;
    if (!alive.current || !metadataRequested.current || checking.current || formRef.current.protocol !== "onvif") return;
    const scope = metadataScope.current;
    const work = (async () => {
      while (metadataIsCurrent(scope) && metadataRequested.current && !checking.current) {
        const profile = pendingMetadataProfile(scope);
        if (!profile) break;
        const source = assignCameraProfile(formRef.current, "main", profile);
        source.onvif_channel_id = String(profile.source_channel ?? source.onvif_channel_id ?? "");
        updateProfileMetadata(profile.token, scope, { metadata_status: { generation: scope.generation, state: "loading" } });
        const request = post("/cameras/test", { ...cameraEditorPayload(source, camera?.id), stream_role: "main", metadata_only: true });
        metadataActive.current = request;
        try {
          const result = await request;
          if (!result?.ok || !result.video) throw new Error("camera_video_not_verified");
          updateProfileMetadata(profile.token, scope, { rtsp_probe: { video: result.video, audio: result.audio }, metadata_status: { generation: scope.generation, state: "done" } });
        } catch (err) {
          updateProfileMetadata(profile.token, scope, { metadata_status: { generation: scope.generation, state: "failed" } });
        } finally { if (metadataActive.current === request) metadataActive.current = null; }
      }
    })();
    metadataRun.current = work;
    try { await work; }
    finally {
      if (metadataRun.current === work) metadataRun.current = null;
      if (alive.current) setMetadataPreparing(Boolean(pendingMetadataProfile()));
      if (alive.current && metadataRequested.current && !checking.current && pendingMetadataProfile()) loadProfileMetadata();
    }
  }
  function requestProfileMetadata() {
    startMetadataScope();
    loadProfileMetadata();
  }
  async function prepareProfileMetadata() {
    if (checking.current) return false;
    const request = revision.current;
    const scope = startMetadataScope();
    if (!scope) return true; // RTSP/manual/offline editing has no ONVIF catalog prerequisite.
    while (stillCurrent(request) && metadataIsCurrent(scope) && pendingMetadataProfile(scope)) {
      await loadProfileMetadata();
    }
    return stillCurrent(request) && metadataIsCurrent(scope);
  }

  async function probe(source, request) {
    if (cameraCheckIsCurrent(checksRef.current.management, cameraManagementKey(source))) return source;
    const result = await post("/cameras/onvif/probe", { ...cameraEditorPayload(source, camera?.id), timeout_seconds: 8 });
    if (!stillCurrent(request)) return null;
    if (!result?.ok || !result.onvif_probe_token) throw new Error("onvif_probe_failed");
    setDevice(result);
    const next = cameraDraftWithProfiles(formRef.current, result);
    replaceForm({ ...next, onvif_probe_token: result.onvif_probe_token });
    rememberCheck("management", { token: result.onvif_probe_token, key: cameraManagementKey(next), at: Date.now() });
    startMetadataScope({ restart: true });
    return formRef.current;
  }

  async function testStream(source, role, request, force = false, image = false) {
    if (!stillCurrent(request)) return false;
    const key = cameraStreamKey(source, role);
    const existing = checkCache.current.get(key);
    const fromCache = !force && cameraCheckIsCurrent(existing, key) && (!image || Boolean(existing.result?.preview_url));
    let result;
    try {
      result = fromCache ? existing.result
        : await post("/cameras/test", { ...cameraEditorPayload(source, camera?.id), stream_role: role, include_preview: image });
    } catch (err) {
      if (stillCurrent(request)) setStreamErrors((previous) => ({ ...previous, [role]: { key, message: cameraEditorError(err, copy) } }));
      throw err;
    }
    if (!stillCurrent(request)) {
      if (!fromCache && result?.preview_token) discardFrames([result.preview_token]);
      return false;
    }
    if (!result?.ok || !result.video || result.tested_role !== role || !result.validation_token) {
      if (result?.preview_token) discardFrames([result.preview_token]);
      throw new Error("camera_video_not_verified");
    }
    if (result.preview_token) temporaryFrames.current.add(result.preview_token);
    const confirmed = fromCache ? existing : { key, at: Date.now(), token: result.validation_token, result };
    checkCache.current.set(key, confirmed);
    rememberCheck(role, confirmed);
    setStreamErrors((previous) => ({ ...previous, [role]: null }));
    const token = source[role === "main" ? "onvif_profile_token" : "onvif_sub_profile_token"];
    setDevice((previous) => previous ? { ...previous, profiles: (previous.profiles || []).map((profile) => (
      profile.token === token && (!fromCache || !profile.rtsp_probe) ? { ...profile,
        rtsp_probe: { video: result.video, audio: result.audio },
        metadata_status: { generation: metadataScope.current?.generation, state: "done" } } : profile
    )) } : previous);
    const next = { ...formRef.current, [`${role}_validation_token`]: result.validation_token };
    if (role === "main") next.validation_token = result.validation_token;
    if (image && result.preview_url) {
      const frame = { ...result, role, identity: cameraPreviewIdentity(source, deviceRef.current) };
      previewCache.current.set(frame.identity, frame); showPreview(frame);
    }
    next.preview_token = previewRef.current?.preview_token || null;
    replaceForm(next);
    collectUnusedFrames();
    return true;
  }

  async function check({ all = false, force = false, automatic = false, image = !all } = {}) {
    if (checking.current) return new Promise((resolve) => {
      queuedCheck.current?.resolve(false);
      queuedCheck.current = { revision: revision.current, options: { all, force, automatic, image }, resolve };
    });
    const problem = cameraEditorProblem(formRef.current, copy, "connection");
    if (problem) { setError(problem); return false; }
    const request = revision.current;
    checking.current = true;
    setOperation(automatic ? image ? "preview" : "video" : all ? "video" : "connection"); setError(""); setNotice("");
    try {
      // Do not run a foreground probe concurrently with background catalog reads.
      if (metadataActive.current) await metadataActive.current.catch(() => {});
      if (!stillCurrent(request)) return false;
      let source = formRef.current;
      if (source.protocol === "onvif") {
        source = await probe(source, request);
        if (!source || !stillCurrent(request)) return false;
        startMetadataScope();
      }
      const roles = cameraStreamRoles(source);
      if (!roles.length) { setError(copy.editorPathRequired); return false; }
      if (cameraSourceConflict(source, deviceRef.current)) { setError(copy.editorSourceConflict); return false; }
      const imageRole = roles.includes("main") ? "main" : roles[0];
      for (const role of all ? roles : [imageRole]) {
        if (!await testStream(formRef.current, role, request, force, image && role === imageRole)) return false;
      }
      if (stillCurrent(request)) setNotice(all ? copy.editorVideoChecked : copy.editorConnected);
      return stillCurrent(request);
    } catch (err) {
      if (stillCurrent(request)) setError(cameraEditorError(err, copy));
      return false;
    } finally {
      checking.current = false;
      if (alive.current) {
        setOperation("");
        const queued = queuedCheck.current; queuedCheck.current = null;
        if (queued?.revision === revision.current) check(queued.options).then(queued.resolve);
        else { queued?.resolve(false); loadProfileMetadata(); }
      }
    }
  }

  async function next() {
    const request = revision.current;
    setError("");
    if (settingsDirty) { setError(copy.editorDeviceUnsaved); return; }
    const problem = cameraEditorProblem(formRef.current, copy, section === "connection" ? "connection" : "all");
    if (problem) { setError(problem); return; }
    if (cameraSourceConflict(formRef.current, device)) { setError(copy.editorSourceConflict); return; }
    if (section === "connection") {
      const role = cameraStreamRoles(formRef.current)[0];
      if (!formRef.current.manual_confirm_unverified && (
        !cameraCheckIsCurrent(checksRef.current[role], cameraStreamKey(formRef.current, role))
        || (formRef.current.protocol === "onvif" && !cameraCheckIsCurrent(checksRef.current.management, cameraManagementKey(formRef.current)))
      ) && !await check()) return;
      if (!stillCurrent(request) || !await prepareProfileMetadata() || !stillCurrent(request)) return;
      setSection("video");
      if (!formRef.current.manual_confirm_unverified) check({ all: true, automatic: true, image: false });
    } else {
      if (!formRef.current.manual_confirm_unverified && !cameraChecksReady(formRef.current, checksRef.current) && !await check({ all: true })) return;
      setSection("review");
    }
  }

  async function save() {
    if (operation || checking.current) return;
    setError("");
    const problem = cameraEditorProblem(formRef.current, copy);
    if (problem) { setError(problem); return; }
    if (settingsDirty) { setError(copy.editorDeviceUnsaved); return; }
    if (cameraConnectionChanged(formRef.current, baseline.current, camera?.id) && cameraSourceConflict(formRef.current, device)) {
      setError(copy.editorSourceConflict); return;
    }
    if ((deviceVideoChanged || cameraConnectionChanged(formRef.current, baseline.current, camera?.id))
      && !formRef.current.manual_confirm_unverified
      && !cameraChecksReady(formRef.current, checksRef.current)
      && !await check({ all: true })) return;
    setOperation("saving");
    try {
      const saved = await apiFetch(camera ? `/cameras/${camera.id}` : "/cameras", {
        method: camera ? "PUT" : "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cameraSavePayload(formRef.current, baseline.current, camera?.id)),
      });
      if (alive.current) onSaved(saved);
    } catch (err) {
      if (alive.current) setError(cameraEditorError(err, copy));
    } finally { if (alive.current) setOperation(""); }
  }

  function invalidateVideo() {
    revision.current += 1;
    setDeviceVideoChanged(true);
    checkCache.current.clear();
    setDevice((previous) => previous ? { ...previous, profiles: (previous.profiles || []).map(({ rtsp_probe, metadata_status, ...profile }) => profile) } : previous);
    checksRef.current = { management: checksRef.current.management };
    setChecks(checksRef.current);
    replaceForm({ ...formRef.current, preview_token: previewRef.current?.preview_token || null, validation_token: null, main_validation_token: null, sub_validation_token: null });
    startMetadataScope({ restart: true, refresh: true });
    loadProfileMetadata();
    setNotice(copy.editorVideoChanged);
  }

  function refreshProfile(result) {
    const current = onvifConfigFromResult(result);
    setDevice((previous) => previous ? { ...previous, profiles: (previous.profiles || []).map((profile) => (
      profile.token === result.profile_token ? { ...profile, current, settings: current, supported: result.supported || {} } : profile
    )) } : previous);
  }

  const connectionChanged = deviceVideoChanged || cameraConnectionChanged(form, baseline.current, camera?.id);
  return {
    form, patch, section, setSection, baseline: baseline.current,
    dirty: Object.keys(cameraEditorChanges(form, baseline.current)).length > 0 || settingsDirty,
    checks, device, discovery, preview, operation, searching, error, notice, streamErrors,
    setError, settingsDirty, setSettingsDirty, selectProfile, selectPurpose, selectSource, selectCandidate, discover, check, next, save,
    invalidateVideo, refreshProfile, requestProfileMetadata, prepareProfileMetadata, metadataPreparing, connectionChanged, verified: cameraChecksReady(form, checks),
    connectionReady: cameraStreamRoles(form).some((role) => cameraCheckIsCurrent(checks[role], cameraStreamKey(form, role)))
      && (form.protocol !== "onvif" || cameraCheckIsCurrent(checks.management, cameraManagementKey(form))),
  };
}
