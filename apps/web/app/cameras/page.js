"use client";

import { useEffect, useRef, useState } from "react";
import Layout from "../../components/Layout";
import AuthenticatedPreviewImage from "../../components/AuthenticatedPreviewImage";
import CameraEditor from "../../components/cameras/CameraEditor";
import { CheckIcon, EditIcon, PowerIcon, TrashIcon } from "../../components/CompactActionIcons";
import OperatorProblemBanners from "../../components/OperatorProblemBanners";
import { OperationDialog, OperationToast } from "../../components/OperationFeedback";
import { apiFetch } from "../../lib/api";
import { useCurrentUser } from "../../lib/currentUser";
import { loadCamerasViewMode, saveCamerasViewMode } from "../../lib/cameraStage16";
import { cameraRuntimeWarningKey, notifyCameraRuntimeChanged } from "../../lib/operatorWarnings";
import { useI18n, useLocaleText } from "../../lib/i18n";

function normalizeCameraError(message, copy) {
  const text = String(message || "").trim();
  if (!text) return copy.actionFailed;
  if (text.includes("camera_validation_required")) {
    return copy.validationRequired;
  }
  if (text.includes("ffprobe") || text.includes("Invalid data") || text.includes("Server returned")) {
    return copy.badCameraResponse;
  }
  if (text.length > 180) {
    return copy.cameraConnectFailed;
  }
  return text;
}

function normalizeRuntimeError(message, copy) {
  const text = String(message || "").trim();
  if (!text) return "";
  if (text.includes("401") || text.toLowerCase().includes("auth")) {
    return copy.authError;
  }
  if (text.toLowerCase().includes("timeout")) {
    return copy.timeoutError;
  }
  if (text.toLowerCase().includes("storage")) {
    return copy.storageError;
  }
  if (text.toLowerCase().includes("ffmpeg")) {
    return copy.ffmpegError;
  }
  if (text.length > 140) {
    return copy.recordingUnavailable;
  }
  return text;
}

function archiveCleanupMessage(result, copy) {
  const warnings = result?.warnings || result?.archive_cleanup?.warnings || result?.recordings?.warnings || [];
  const reasons = result?.archive_cleanup?.reason_counts || result?.recordings?.reason_counts || {};
  if (!result?.camera_removed) return "";
  if (!warnings.length && !Object.keys(reasons).length) return copy.cameraDeleted;
  if (reasons.delete_recordings_permission_missing) {
    return copy.archiveDeleteNoPermission;
  }
  if (reasons.active_job) {
    return copy.archiveDeleteActiveJob;
  }
  if (reasons.unowned) {
    return copy.archiveDeleteUnowned;
  }
  if (reasons.path_outside_storage) {
    return copy.archiveDeleteUnsafePath;
  }
  if (warnings.includes("archive_retained") || reasons.recordings_exist_delete_files_false_requires_safe_policy) {
    return copy.archiveRetained;
  }
  return copy.archiveCleanupPartial;
}

function cameraEndpointLabel(camera) {
  const protocol = String(camera?.protocol || "").toUpperCase();
  return `${camera?.host || "-"}:${camera?.port || "-"} · ${protocol || "-"}`;
}

function cameraRecordingLabel(camera, copy) {
  if (camera?.recording_mode === "always") return copy.always;
  return camera?.recording_mode || "-";
}

function cameraStreamsLabel(camera, copy = {}) {
  const rec = String(camera?.default_record_stream || "main").toUpperCase();
  const live = String(camera?.default_live_stream || "main").toUpperCase();
  return copy.streamCountSummary
    ? copy.streamCountSummary.replace("{rec}", rec).replace("{live}", live)
    : `${rec} · ${live}`;
}

function getCameraRuntimeBadge(camera, runtime, recorderStatus, storageAvailable, copy) {
  if (!camera.enabled || runtime?.enabled === false || camera.status === "disabled") {
    return { text: copy.disabled, cls: "warn" };
  }

  if (storageAvailable === false) {
    return { text: copy.error, cls: "err" };
  }

  if (!recorderStatus) {
    if (camera.status === "error") return { text: copy.error, cls: "err" };
    return { text: copy.unknownStatus, cls: "warn" };
  }

  const runtimeError = normalizeRuntimeError(runtime?.last_error || runtime?.camera_last_error || recorderStatus?.last_error, copy);
  const jobState = String(runtime?.job_state || camera.status || "").toLowerCase();
  const currentFailure = runtime?.current_failure === true;
  const confirmedRecording = runtime?.confirmed_recording === true;
  const staleCurrentSegment = runtime?.stale_current_segment === true || String(runtime?.recording_health || "").toLowerCase() === "degraded";

  if (recorderStatus?.heartbeat?.status === "stale_or_unavailable") {
    return { text: copy.unknownStatus, cls: "warn" };
  }

  if (jobState === "restarting") return { text: copy.restarting, cls: "warn" };
  if (jobState === "starting") return { text: copy.starting, cls: "warn" };
  if (jobState === "stopping") return { text: copy.stopping, cls: "warn" };
  if (jobState === "recording" && staleCurrentSegment) return { text: copy.recordingStale, cls: "warn" };
  if (jobState === "recording" && confirmedRecording && !currentFailure) return { text: copy.recordingNow, cls: "ok" };
  if (jobState === "recording" && !confirmedRecording && !currentFailure) return { text: copy.starting, cls: "warn" };
  if (currentFailure || runtimeError || jobState === "error") {
    return { text: copy.error, cls: "err" };
  }
  if (jobState === "recording" && confirmedRecording) return { text: copy.recordingNow, cls: "ok" };
  if (runtime?.recording_mode && runtime.recording_mode !== "always") {
    return { text: copy.recordingWaiting, cls: "warn" };
  }

  return { text: copy.recordingWaiting, cls: "warn" };
}

export default function CamerasPage() {
  const { t } = useI18n();
  const copy = useLocaleText("cameras");
  const { currentUser } = useCurrentUser();
  const [cameras, setCameras] = useState([]);
  const [camerasLoadState, setCamerasLoadState] = useState("idle");
  const [viewMode, setViewMode] = useState("list");
  const [storage, setStorage] = useState(null);
  const [recorderStatus, setRecorderStatus] = useState(null);
  const [secondaryStatusState, setSecondaryStatusState] = useState("idle");
  const [showEditor, setShowEditor] = useState(false);
  const [editorCamera, setEditorCamera] = useState(null);
  const [error, setError] = useState("");
  const [deleteWarning, setDeleteWarning] = useState("");
  const [busy, setBusy] = useState(false);
  const [operationToast, setOperationToast] = useState(null);

  const [deleteModalOpen, setDeleteModalOpen] = useState(false);
  const [cameraToDelete, setCameraToDelete] = useState(null);
  const [deleteFiles, setDeleteFiles] = useState(false);
  const viewModeLoadedRef = useRef(false);
  const recorderWarningKeyRef = useRef(null);
  const camerasRequestRef = useRef(0);
  const secondaryRequestRef = useRef(0);

  async function load() {
    const request = ++camerasRequestRef.current;
    setCamerasLoadState((prev) => (prev === "loaded" || prev === "refreshing" ? "refreshing" : "loading"));
    try {
      const cams = await apiFetch("/cameras");
      if (request !== camerasRequestRef.current) return;
      setCameras(cams);
      setError("");
      setCamerasLoadState("loaded");
    } catch (err) {
      if (request !== camerasRequestRef.current) return;
      setError(normalizeCameraError(err.message, copy));
      setCamerasLoadState((prev) => (prev === "loaded" || prev === "refreshing" ? "loaded" : "error"));
    }
  }

  async function loadSecondaryStatus() {
    const request = ++secondaryRequestRef.current;
    setSecondaryStatusState((prev) => (prev === "loaded" || prev === "refreshing" ? "refreshing" : "loading"));
    try {
      const [st, recorder] = await Promise.all([
        apiFetch("/storage/status").catch(() => null),
        apiFetch("/system/recorder/summary").catch(() => null),
      ]);
      if (request !== secondaryRequestRef.current) return;
      setStorage(st);
      setRecorderStatus(recorder);
      setSecondaryStatusState("loaded");
      const key = cameraRuntimeWarningKey(recorder);
      if (key !== null) {
        if (recorderWarningKeyRef.current !== null && recorderWarningKeyRef.current !== key) notifyCameraRuntimeChanged();
        recorderWarningKeyRef.current = key;
      }
    } catch (_) {
      if (request !== secondaryRequestRef.current) return;
      setSecondaryStatusState("error");
    }
  }

  const camerasLoaded = camerasLoadState === "loaded" || camerasLoadState === "refreshing";
  const camerasFirstLoading = camerasLoadState === "idle" || camerasLoadState === "loading";
  const storagePathChecks = storage?.storage_path_checks || {};
  const storageAvailable = storagePathChecks.path_exists ?? storage?.storage_root_exists;
  const recorderCameraMap = new Map(
    (recorderStatus?.camera_recording_states || []).map((item) => [String(item.camera_id), item])
  );

  useEffect(() => {
    load();
    loadSecondaryStatus();
    const timer = setInterval(load, 5000);
    const secondaryTimer = setInterval(loadSecondaryStatus, 5000);
    return () => {
      camerasRequestRef.current += 1;
      secondaryRequestRef.current += 1;
      clearInterval(timer);
      clearInterval(secondaryTimer);
    };
  }, []);

  useEffect(() => {
    setViewMode(loadCamerasViewMode(window.localStorage, currentUser, "list"));
    viewModeLoadedRef.current = true;
  }, [currentUser?.id, currentUser?.username]);

  useEffect(() => {
    if (!viewModeLoadedRef.current) return;
    saveCamerasViewMode(window.localStorage, currentUser, viewMode);
  }, [currentUser?.id, currentUser?.username, viewMode]);

  function openCreate() {
    setEditorCamera(null);
    setShowEditor(true);
  }

  function openEdit(camera) {
    setEditorCamera(camera);
    setShowEditor(true);
  }

  async function cameraSaved() {
    setShowEditor(false);
    setEditorCamera(null);
    await Promise.all([load(), loadSecondaryStatus()]);
    notifyCameraRuntimeChanged();
  }

  function openDeleteModal(camera) {
    setCameraToDelete(camera);
    setDeleteFiles(false);
    setDeleteModalOpen(true);
  }

  function closeDeleteModal() {
    setDeleteModalOpen(false);
    setCameraToDelete(null);
    setDeleteFiles(false);
  }

  async function toggleCamera(camera) {
    setError("");
    try {
      await apiFetch(`/cameras/${camera.id}/${camera.enabled ? "disable" : "enable"}`, {
        method: "POST",
      });
      await Promise.all([load(), loadSecondaryStatus()]);
      notifyCameraRuntimeChanged();
    } catch (err) {
      setError(normalizeCameraError(err.message, copy));
    }
  }

  async function confirmDeleteCamera() {
    if (!cameraToDelete || busy) return;

    setError("");
    setBusy(true);
    try {
      const result = await apiFetch(
        `/cameras/${cameraToDelete.id}?delete_files=${deleteFiles ? "true" : "false"}&operation_id=${encodeURIComponent(newCameraDeleteOperationId())}`,
        { method: "DELETE" }
      );
      if (!result?.camera_removed) {
        closeDeleteModal();
        setError(copy.actionFailed);
        return;
      }
      const cleanupMessage = archiveCleanupMessage(result, copy) || copy.cameraDeleted;
      const cleanupPartial = deleteFiles && (
        result?.status === "deleted_archive_cleanup_partial"
        || cleanupMessage !== copy.cameraDeleted
      );
      closeDeleteModal();
      if (cleanupPartial) {
        setDeleteWarning(cleanupMessage);
      } else {
        setDeleteWarning("");
        setOperationToast({
          id: `camera-delete-${Date.now()}`,
          title: copy.cameraDeleted,
          message: cleanupMessage === copy.cameraDeleted ? "" : cleanupMessage,
          tone: "success",
        });
      }
      await Promise.all([load(), loadSecondaryStatus()]);
      notifyCameraRuntimeChanged();
    } catch (err) {
      setError(normalizeCameraError(err.message, copy));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Layout>
      <div className="standardPage">
      <div className="pageHeader cameraPageHeader">
        <div>
          <h1 className="pageTitle">{copy.title}</h1>
          <div className="pageSubtitle">{copy.subtitle}</div>
        </div>
        <div className="cameraHeaderActions">
          <div className="cameraViewToggle" role="group" aria-label={copy.displayMode}>
            <button type="button" className={viewMode === "list" ? "active" : ""} onClick={() => setViewMode("list")}>{copy.list}</button>
            <button type="button" className={viewMode === "cards" ? "active" : ""} onClick={() => setViewMode("cards")}>{copy.cards}</button>
          </div>
          <button className="button" onClick={openCreate}>{copy.addCamera}</button>
        </div>
      </div>

      <OperatorProblemBanners domains={["cameras", "recorder", "live"]} className="pageWarnings cameraPageWarnings" limit={4} />

      {error && !showEditor ? <div className="badge err" style={{ marginBottom: 14 }}>{error}</div> : null}
      {deleteWarning && !showEditor ? <div className="badge warn" style={{ marginBottom: 14 }}>{deleteWarning}</div> : null}

      <div className={viewMode === "cards" ? "cameraTileGrid" : "cameraCards"}>
        {camerasFirstLoading ? (
          <div className="card">{t("common.loading")}</div>
        ) : camerasLoadState === "error" ? (
          <div className="card">{error}</div>
        ) : camerasLoaded && !cameras.length ? (
          <div className="card">{copy.noCameras}</div>
        ) : (
          cameras.map((camera) => {
            const runtime = recorderCameraMap.get(String(camera.id));
            const badge = getCameraRuntimeBadge(camera, runtime, recorderStatus, storageAvailable, copy);
            if (viewMode === "cards") {
              return (
                <article className="cameraTileCard" key={camera.id}>
                  <div className="cameraTilePreview">
                    {camera.preview_url ? (
                      <AuthenticatedPreviewImage
                        src={camera.preview_url}
                        fallback={<div className="cameraTilePreviewEmpty">{copy.noFrame}</div>}
                      />
                    ) : (
                      <div className="cameraTilePreviewEmpty">{copy.noFrame}</div>
                    )}
                  </div>
                  <div className="cameraTileBody">
                    <div className="cameraTileTitleRow">
                      <div className="cameraTileIdentity">
                        <div className="cameraTileName" title={camera.name}>{camera.name}</div>
                        <div className="cameraTileEndpoint" title={cameraEndpointLabel(camera)}>{cameraEndpointLabel(camera)}</div>
                      </div>
                      <div className="cameraTileActions">
                        <button className="cameraTileIconButton" onClick={() => openEdit(camera)} title={copy.edit} aria-label={copy.edit}>
                          <EditIcon />
                        </button>
                        <button className="cameraTileIconButton" onClick={() => toggleCamera(camera)} title={camera.enabled ? copy.disable : copy.enable} aria-label={camera.enabled ? copy.disable : copy.enable}>
                          {camera.enabled ? <PowerIcon /> : <CheckIcon />}
                        </button>
                        <button className="cameraTileIconButton danger" onClick={() => openDeleteModal(camera)} title={copy.delete} aria-label={copy.delete}>
                          <TrashIcon />
                        </button>
                      </div>
                    </div>
                    <div className="cameraTileSystem">
                      <div><span>{copy.recording}</span><strong>{cameraRecordingLabel(camera, copy)}</strong></div>
                      <div><span>{copy.segment}</span><strong>{camera.segment_minutes} {copy.minutesShort}</strong></div>
                      <div><span>{copy.retention}</span><strong>{camera.retention_days} {copy.daysShort}</strong></div>
                      <div><span>{copy.limit}</span><strong>{camera.storage_quota_gb} {copy.gbShort}</strong></div>
                    </div>
                    <div className="cameraTileFoot">
                      <span>{cameraStreamsLabel(camera, copy)}</span>
                      <span className={`badge ${badge.cls}`}>{badge.text}</span>
                    </div>
                  </div>
                </article>
              );
            }
            return (
              <div className="cameraCard card" key={camera.id}>
                <div className="cameraCardGrid">
                  <div className="cameraField cameraNameField">
                    <div className="cameraFieldLabel">{copy.camera}</div>
                    <div className="cameraPrimary" title={camera.name}>{camera.name}</div>
                    <div className="cameraSecondary" title={`${copy.folder}: ${camera.storage_folder_name}`}>{copy.folder}: {camera.storage_folder_name}</div>
                    <div className="cameraActions">
                      <button className="cameraIconButton" onClick={() => openEdit(camera)} title={copy.edit} aria-label={copy.edit}>
                        <EditIcon />
                      </button>
                      <button className="cameraIconButton" onClick={() => toggleCamera(camera)} title={camera.enabled ? copy.disable : copy.enable} aria-label={camera.enabled ? copy.disable : copy.enable}>
                        {camera.enabled ? <PowerIcon /> : <CheckIcon />}
                      </button>
                      <button className="cameraIconButton danger" onClick={() => openDeleteModal(camera)} title={copy.delete} aria-label={copy.delete}>
                        <TrashIcon />
                      </button>
                    </div>
                  </div>

                  <div className="cameraPreviewField" aria-label={copy.cameraFrame}>
                    {camera.preview_url ? (
                      <AuthenticatedPreviewImage
                        src={camera.preview_url}
                        fallback={<div className="cameraPreviewPlaceholder">{copy.noFrame}</div>}
                      />
                    ) : (
                      <div className="cameraPreviewPlaceholder">{copy.noFrame}</div>
                    )}
                  </div>

                  <div className="cameraMetaGrid">
                    <div className="cameraField">
                      <div className="cameraFieldLabel">{copy.protocol}</div>
                      <div>{camera.protocol?.toUpperCase()}</div>
                    </div>

                    <div className="cameraField">
                      <div className="cameraFieldLabel">{copy.address}</div>
                      <div>{camera.host}:{camera.port}</div>
                    </div>

                    <div className="cameraField">
                      <div className="cameraFieldLabel">{copy.segment}</div>
                      <div>{camera.segment_minutes} {copy.minutesShort}</div>
                    </div>

                    <div className="cameraField">
                      <div className="cameraFieldLabel">{copy.retention}</div>
                      <div>{camera.retention_days} {copy.daysShort}</div>
                    </div>

                    <div className="cameraField">
                      <div className="cameraFieldLabel">{copy.limit}</div>
                      <div>{camera.storage_quota_gb} {copy.gbShort}</div>
                    </div>

                    <div className="cameraField">
                      <div className="cameraFieldLabel">{copy.status}</div>
                      <div className="cameraStatusStack">
                        <span className={`badge ${badge.cls}`}>{badge.text}</span>
                      </div>
                    </div>
                  </div>
                </div>

              </div>
            );
          })
        )}
      </div>

      {showEditor ? <CameraEditor camera={editorCamera} onClose={() => setShowEditor(false)} onSaved={cameraSaved} /> : null}

      <OperationDialog
        dialog={deleteModalOpen ? {
          id: `camera-delete-${cameraToDelete?.id || "current"}`,
          presentation: "compact-confirmation",
          title: copy.deleteTitle,
          message: `${copy.cameraLabel}: ${cameraToDelete?.name || ""}`,
          content: (
            <>
              <label style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <input type="checkbox" checked={deleteFiles} onChange={(e) => setDeleteFiles(e.target.checked)} disabled={busy} />
                <span>{copy.deleteRecordsFolder}</span>
              </label>
              <div className="badge warn">{copy.deleteHint}</div>
            </>
          ),
          busy,
          dismissible: !busy,
          cancelLabel: copy.cancel,
          closeLabel: copy.close,
          confirmLabel: busy ? copy.deleting : copy.delete,
          confirmTone: "danger",
          tone: "error",
          onConfirm: confirmDeleteCamera,
        } : null}
        onClose={closeDeleteModal}
      />
      <OperationToast toast={operationToast} onClose={() => setOperationToast(null)} />
      </div>
    </Layout>
  );
}
function newCameraDeleteOperationId() {
  const randomPart = globalThis.crypto?.randomUUID
    ? globalThis.crypto.randomUUID().replaceAll("-", "")
    : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 14)}`;
  return `camera-delete-${randomPart}`;
}
