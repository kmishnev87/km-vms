"use client";

import { useEffect, useState } from "react";
import Layout from "../../components/Layout";
import { apiFetch } from "../../lib/api";

const initialForm = {
  name: "",
  enabled: true,
  protocol: "rtsp",
  host: "",
  port: 554,
  username: "",
  password: "",
  rtsp_main_url: "",
  rtsp_sub_url: "",
  rtsp_transport: "tcp",
  onvif_path: "",
  onvif_profile_token: "",
  onvif_channel_id: "",
  recording_mode: "always",
  default_live_stream: "sub",
  default_record_stream: "main",
  segment_minutes: 5,
  retention_days: 30,
  storage_quota_gb: 50,
};

const initialOnvifConfig = {
  codec: "",
  width: "",
  height: "",
  fps: "",
  bitrate: "",
  iframe_interval: "",
  quality: "",
};

function getStatusBadge(camera) {
  if (!camera.enabled) return { text: "Отключена", cls: "warn" };
  if (camera.status === "recording") return { text: "Идёт запись", cls: "ok" };
  if (camera.status === "error") return { text: "Ошибка", cls: "err" };
  if (camera.status === "created" || camera.status === "enabled") return { text: "Включена", cls: "ok" };
  if (camera.status === "disabled") return { text: "Отключена", cls: "warn" };
  return { text: camera.status || "Неизвестно", cls: "warn" };
}

function prettyRtspValue(value) {
  if (!value) return "";
  if (value.toLowerCase().startsWith("rtsp://")) {
    try {
      const url = new URL(value);
      return `${url.pathname || ""}${url.search || ""}`;
    } catch {
      return value;
    }
  }
  return value;
}

export default function CamerasPage() {
  const [cameras, setCameras] = useState([]);
  const [storage, setStorage] = useState(null);
  const [showEditor, setShowEditor] = useState(false);
  const [editorMode, setEditorMode] = useState("create");
  const [editingCameraId, setEditingCameraId] = useState(null);
  const [form, setForm] = useState(initialForm);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [testing, setTesting] = useState(false);
  const [onvifBusy, setOnvifBusy] = useState(false);
  const [onvifData, setOnvifData] = useState(null);
  const [onvifConfigBusy, setOnvifConfigBusy] = useState(false);
  const [onvifConfig, setOnvifConfig] = useState(initialOnvifConfig);

  const [deleteModalOpen, setDeleteModalOpen] = useState(false);
  const [cameraToDelete, setCameraToDelete] = useState(null);
  const [deleteFiles, setDeleteFiles] = useState(false);

  async function load() {
    try {
      const [cams, st] = await Promise.all([
        apiFetch("/cameras"),
        apiFetch("/storage/status"),
      ]);
      setCameras(cams);
      setStorage(st);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  function patch(key, value) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function patchOnvifConfig(key, value) {
    setOnvifConfig((prev) => ({ ...prev, [key]: value }));
  }

  function openCreate() {
    setEditorMode("create");
    setEditingCameraId(null);
    setForm(initialForm);
    setError("");
    setTestResult(null);
    setOnvifData(null);
    setOnvifConfig(initialOnvifConfig);
    setShowEditor(true);
  }

  function openEdit(camera) {
    setEditorMode("edit");
    setEditingCameraId(camera.id);
    setForm({
      name: camera.name || "",
      enabled: camera.enabled ?? true,
      protocol: camera.protocol || "rtsp",
      host: camera.host || "",
      port: camera.port || 554,
      username: camera.username || "",
      password: "",
      rtsp_main_url: prettyRtspValue(camera.rtsp_main_url || ""),
      rtsp_sub_url: prettyRtspValue(camera.rtsp_sub_url || ""),
      rtsp_transport: camera.rtsp_transport || "tcp",
      onvif_path: camera.onvif_path || "",
      onvif_profile_token: camera.onvif_profile_token || "",
      onvif_channel_id: camera.onvif_channel_id || "",
      recording_mode: camera.recording_mode || "always",
      default_live_stream: camera.default_live_stream || "sub",
      default_record_stream: camera.default_record_stream || "main",
      segment_minutes: camera.segment_minutes || 5,
      retention_days: camera.retention_days || 30,
      storage_quota_gb: camera.storage_quota_gb || 50,
    });
    setError("");
    setTestResult(null);
    setOnvifData(null);
    setOnvifConfig(initialOnvifConfig);
    setShowEditor(true);
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

  async function runTest() {
    setError("");
    setTesting(true);
    setTestResult(null);
    try {
      const payload = {
        ...form,
        camera_id: editingCameraId || null,
        port: Number(form.port),
        storage_quota_gb: Number(form.storage_quota_gb),
        segment_minutes: Number(form.segment_minutes),
        retention_days: Number(form.retention_days),
      };
      const result = await apiFetch("/cameras/test", {
        method: "POST",
        body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" },
      });
      setTestResult(result);
    } catch (err) {
      setError(err.message);
      setTestResult(null);
    } finally {
      setTesting(false);
    }
  }

  async function loadOnvifProfiles() {
    setError("");
    setOnvifBusy(true);
    setOnvifData(null);
    try {
      const payload = {
        camera_id: editingCameraId || null,
        host: form.host,
        port: Number(form.port || 80),
        username: form.username,
        password: form.password,
      };
      const result = await apiFetch("/cameras/onvif/profiles", {
        method: "POST",
        body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" },
      });
      setOnvifData(result);
    } catch (err) {
      setError(err.message);
      setOnvifData(null);
    } finally {
      setOnvifBusy(false);
    }
  }

  function useProfileAsMain(profile) {
    patch("onvif_profile_token", profile.token || "");
    patch("rtsp_main_url", prettyRtspValue(profile.stream_uri || ""));
    patch("default_record_stream", "main");
  }

  function useProfileAsSub(profile) {
    patch("rtsp_sub_url", prettyRtspValue(profile.stream_uri || ""));
    if (!form.onvif_profile_token) {
      patch("onvif_profile_token", profile.token || "");
    }
    patch("default_live_stream", "sub");
  }

  async function loadOnvifProfileConfig() {
    if (!form.onvif_profile_token) {
      setError("Сначала выбери ONVIF профиль");
      return;
    }

    setError("");
    setOnvifConfigBusy(true);
    try {
      const result = await apiFetch("/cameras/onvif/profile_config", {
        method: "POST",
        body: JSON.stringify({
          camera_id: editingCameraId || null,
          host: form.host,
          port: Number(form.port),
          username: form.username,
          password: form.password,
          profile_token: form.onvif_profile_token,
        }),
        headers: { "Content-Type": "application/json" },
      });

      setOnvifConfig({
        codec: result.config?.codec || "",
        width: result.config?.width || "",
        height: result.config?.height || "",
        fps: result.config?.fps || "",
        bitrate: result.config?.bitrate || "",
        iframe_interval: result.config?.iframe_interval || "",
        quality: result.config?.quality || "",
      });
    } catch (err) {
      setError(err.message);
    } finally {
      setOnvifConfigBusy(false);
    }
  }

  async function applyOnvifProfileConfig() {
    if (!form.onvif_profile_token) {
      setError("Сначала выбери ONVIF профиль");
      return;
    }

    setError("");
    setOnvifConfigBusy(true);
    try {
      await apiFetch("/cameras/onvif/update_profile", {
        method: "POST",
        body: JSON.stringify({
          camera_id: editingCameraId || null,
          host: form.host,
          port: Number(form.port),
          username: form.username,
          password: form.password,
          profile_token: form.onvif_profile_token,
          config: {
            codec: onvifConfig.codec,
            width: onvifConfig.width ? Number(onvifConfig.width) : null,
            height: onvifConfig.height ? Number(onvifConfig.height) : null,
            fps: onvifConfig.fps ? Number(onvifConfig.fps) : null,
            bitrate: onvifConfig.bitrate ? Number(onvifConfig.bitrate) : null,
            iframe_interval: onvifConfig.iframe_interval ? Number(onvifConfig.iframe_interval) : null,
            quality: onvifConfig.quality !== "" ? Number(onvifConfig.quality) : null,
          },
        }),
        headers: { "Content-Type": "application/json" },
      });

      await loadOnvifProfileConfig();
      await runTest();
    } catch (err) {
      setError(err.message);
    } finally {
      setOnvifConfigBusy(false);
    }
  }

  async function saveCamera() {
    setError("");
    setBusy(true);
    try {
      const payload = {
        ...form,
        port: Number(form.port),
        segment_minutes: Number(form.segment_minutes),
        retention_days: Number(form.retention_days),
        storage_quota_gb: Number(form.storage_quota_gb),
      };

      if (!payload.password) delete payload.password;

      if (editorMode === "create") {
        await apiFetch("/cameras", {
          method: "POST",
          body: JSON.stringify(payload),
          headers: { "Content-Type": "application/json" },
        });
      } else {
        await apiFetch(`/cameras/${editingCameraId}`, {
          method: "PUT",
          body: JSON.stringify(payload),
          headers: { "Content-Type": "application/json" },
        });
      }

      setShowEditor(false);
      setEditingCameraId(null);
      setForm(initialForm);
      setTestResult(null);
      setOnvifData(null);
      setOnvifConfig(initialOnvifConfig);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function toggleCamera(camera) {
    setError("");
    try {
      await apiFetch(`/cameras/${camera.id}/${camera.enabled ? "disable" : "enable"}`, {
        method: "POST",
      });
      await load();
    } catch (err) {
      setError(err.message);
    }
  }

  async function confirmDeleteCamera() {
    if (!cameraToDelete) return;

    setError("");
    setBusy(true);
    try {
      await apiFetch(
        `/cameras/${cameraToDelete.id}?delete_files=${deleteFiles ? "true" : "false"}`,
        { method: "DELETE" }
      );
      closeDeleteModal();
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Layout>
      <div className="standardPage">
      <div className="pageHeader">
        <div>
          <h1 className="pageTitle">Камеры</h1>
          <div className="pageSubtitle">Добавление, редактирование и управление камерами</div>
        </div>
        <button className="button" onClick={openCreate}>Добавить камеру</button>
      </div>

      {error ? <div className="badge err" style={{ marginBottom: 14 }}>{error}</div> : null}

      <div className="card" style={{ marginBottom: 18 }}>
        <div className="toolbar">
          <div className="badge">Хранилище: {storage?.storage_root || "..."}</div>
          <div className={`badge ${storage?.storage_root_exists ? "ok" : "err"}`}>
            {storage?.storage_root_exists ? "Каталог доступен" : "Каталог недоступен"}
          </div>
          <div className={`badge ${storage?.storage_root_writable ? "ok" : "err"}`}>
            {storage?.storage_root_writable ? "Есть запись" : "Нет записи"}
          </div>
        </div>
      </div>

      <div className="cameraCards">
        {!cameras.length ? (
          <div className="card">Камеры ещё не добавлены.</div>
        ) : (
          cameras.map((camera) => {
            const badge = getStatusBadge(camera);
            return (
              <div className="cameraCard card" key={camera.id}>
                <div className="cameraCardGrid">
                  <div className="cameraField cameraNameField">
                    <div className="cameraFieldLabel">Имя</div>
                    <div className="cameraPrimary">{camera.name}</div>
                    <div className="cameraSecondary">папка: {camera.storage_folder_name}</div>
                  </div>

                  <div className="cameraField">
                    <div className="cameraFieldLabel">Протокол</div>
                    <div>{camera.protocol?.toUpperCase()}</div>
                  </div>

                  <div className="cameraField">
                    <div className="cameraFieldLabel">Адрес</div>
                    <div>{camera.host}:{camera.port}</div>
                  </div>

                  <div className="cameraField">
                    <div className="cameraFieldLabel">Сегм.</div>
                    <div>{camera.segment_minutes} мин</div>
                  </div>

                  <div className="cameraField">
                    <div className="cameraFieldLabel">Хран.</div>
                    <div>{camera.retention_days} дн</div>
                  </div>

                  <div className="cameraField">
                    <div className="cameraFieldLabel">Лимит</div>
                    <div>{camera.storage_quota_gb} ГБ</div>
                  </div>

                  <div className="cameraField">
                    <div className="cameraFieldLabel">Статус</div>
                    <div><span className={`badge ${badge.cls}`}>{badge.text}</span></div>
                  </div>
                </div>

                <div className="cameraActions">
                  <button className="cameraIconButton" onClick={() => openEdit(camera)} title="Редактировать" aria-label="Редактировать">
                    {"\u270e"}
                  </button>
                  <button className="cameraIconButton" onClick={() => toggleCamera(camera)} title={camera.enabled ? "Отключить" : "Включить"} aria-label={camera.enabled ? "Отключить" : "Включить"}>
                    {camera.enabled ? "\u23fb" : "\u2713"}
                  </button>
                  <button className="cameraIconButton danger" onClick={() => openDeleteModal(camera)} title="Удалить" aria-label="Удалить">
                    {"\ud83d\uddd1"}
                  </button>
                </div>
              </div>
            );
          })
        )}
      </div>

      {showEditor ? (
        <div className="modalBackdrop">
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modalHeader">
              <h2 style={{ margin: 0 }}>
                {editorMode === "create" ? "Добавить камеру" : "Редактировать камеру"}
              </h2>
              <button className="iconCloseButton" onClick={() => setShowEditor(false)} aria-label="Закрыть">×</button>
            </div>

            <div className="formGrid">
              <div>
                <div className="formLabel">Имя камеры</div>
                <input className="input" value={form.name} onChange={(e) => patch("name", e.target.value)} />
              </div>

              <div>
                <div className="formLabel">Протокол</div>
                <select className="select" value={form.protocol} onChange={(e) => patch("protocol", e.target.value)}>
                  <option value="rtsp">RTSP</option>
                  <option value="onvif">ONVIF</option>
                </select>
              </div>

              <div>
                <div className="formLabel">IP / Хост</div>
                <input className="input" value={form.host} onChange={(e) => patch("host", e.target.value)} />
              </div>

              <div>
                <div className="formLabel">Порт</div>
                <input className="input" value={form.port} onChange={(e) => patch("port", e.target.value)} />
              </div>

              <div>
                <div className="formLabel">Логин</div>
                <input className="input" value={form.username} onChange={(e) => patch("username", e.target.value)} />
              </div>

              <div>
                <div className="formLabel">Пароль {editorMode === "edit" ? "(оставь пустым, если не менять)" : ""}</div>
                <input className="input" type="password" value={form.password} onChange={(e) => patch("password", e.target.value)} />
              </div>

              <div className="full">
                <div className="formLabel">RTSP Main Path / URL</div>
                <input className="input" value={form.rtsp_main_url} onChange={(e) => patch("rtsp_main_url", e.target.value)} />
              </div>

              <div className="full">
                <div className="formLabel">RTSP Sub Path / URL</div>
                <input className="input" value={form.rtsp_sub_url} onChange={(e) => patch("rtsp_sub_url", e.target.value)} />
              </div>

              <div>
                <div className="formLabel">RTSP Transport</div>
                <select className="select" value={form.rtsp_transport} onChange={(e) => patch("rtsp_transport", e.target.value)}>
                  <option value="tcp">TCP</option>
                  <option value="udp">UDP</option>
                </select>
              </div>

              <div>
                <div className="formLabel">Режим записи</div>
                <select className="select" value={form.recording_mode} onChange={(e) => patch("recording_mode", e.target.value)}>
                  <option value="always">Постоянно</option>
                </select>
              </div>

              <div>
                <div className="formLabel">Поток для live</div>
                <select className="select" value={form.default_live_stream} onChange={(e) => patch("default_live_stream", e.target.value)}>
                  <option value="sub">Sub</option>
                  <option value="main">Main</option>
                </select>
              </div>

              <div>
                <div className="formLabel">Поток для записи</div>
                <select className="select" value={form.default_record_stream} onChange={(e) => patch("default_record_stream", e.target.value)}>
                  <option value="main">Main</option>
                  <option value="sub">Sub</option>
                </select>
              </div>

              <div>
                <div className="formLabel">Длительность сегмента</div>
                <select className="select" value={form.segment_minutes} onChange={(e) => patch("segment_minutes", e.target.value)}>
                  <option value="5">5 минут</option>
                  <option value="15">15 минут</option>
                  <option value="30">30 минут</option>
                  <option value="45">45 минут</option>
                  <option value="60">60 минут</option>
                  <option value="120">120 минут</option>
                </select>
              </div>

              <div>
                <div className="formLabel">Срок хранения</div>
                <select className="select" value={form.retention_days} onChange={(e) => patch("retention_days", e.target.value)}>
                  <option value="7">7 дней</option>
                  <option value="14">14 дней</option>
                  <option value="21">21 день</option>
                  <option value="30">30 дней</option>
                </select>
              </div>

              <div>
                <div className="formLabel">Лимит архива (ГБ, минимум 50)</div>
                <input className="input" type="number" min="50" value={form.storage_quota_gb} onChange={(e) => patch("storage_quota_gb", e.target.value)} />
              </div>

              <div className="full">
                <div className="formLabel">ONVIF Path</div>
                <input className="input" value={form.onvif_path} onChange={(e) => patch("onvif_path", e.target.value)} />
              </div>

              <div>
                <div className="formLabel">ONVIF Profile Token</div>
                <input className="input" value={form.onvif_profile_token} onChange={(e) => patch("onvif_profile_token", e.target.value)} />
              </div>

              <div>
                <div className="formLabel">ONVIF Channel ID</div>
                <input className="input" value={form.onvif_channel_id} onChange={(e) => patch("onvif_channel_id", e.target.value)} />
              </div>
            </div>

            <div className="card" style={{ marginTop: 18 }}>
              <div className="toolbar" style={{ marginBottom: 12 }}>
                <button className="button secondary small" onClick={runTest} disabled={testing}>
                  {testing ? "Тестируем..." : "Тест"}
                </button>

                {form.protocol === "onvif" ? (
                  <button className="button secondary small" onClick={loadOnvifProfiles} disabled={onvifBusy}>
                    {onvifBusy ? "Читаем ONVIF..." : "ONVIF профили"}
                  </button>
                ) : null}
              </div>

              {testResult ? (
                <div style={{ fontSize: 14, lineHeight: 1.7, marginBottom: form.protocol === "onvif" ? 16 : 0 }}>
                  <div><strong>Тест:</strong> OK</div>
                  <div><strong>URL:</strong> {testResult.input_url_used}</div>
                  <div><strong>Транспорт:</strong> {testResult.transport}</div>
                  <div><strong>Видео:</strong> {testResult.video ? `${testResult.video.codec || "-"}, ${testResult.video.width || "-"}x${testResult.video.height || "-"}, fps ${testResult.video.fps || "-"}` : "нет"}</div>
                  <div><strong>Аудио:</strong> {testResult.audio ? `${testResult.audio.codec || "-"}, channels ${testResult.audio.channels || "-"}, sample_rate ${testResult.audio.sample_rate || "-"}` : "нет"}</div>
                  <div><strong>Формат:</strong> {testResult.format?.format_name || "-"}</div>
                  <div><strong>Битрейт:</strong> {testResult.format?.bit_rate || "-"}</div>
                </div>
              ) : (
                <div style={{ color: "#64748b", fontSize: 14, marginBottom: form.protocol === "onvif" ? 16 : 0 }}>
                  Нажми «Тест», чтобы проверить подключение и получить параметры потока.
                </div>
              )}

              {form.protocol === "onvif" && onvifData ? (
                <div>
                  <div style={{ marginBottom: 12, fontSize: 14, lineHeight: 1.7 }}>
                    <div><strong>Производитель:</strong> {onvifData.device?.manufacturer || "-"}</div>
                    <div><strong>Модель:</strong> {onvifData.device?.model || "-"}</div>
                    <div><strong>Прошивка:</strong> {onvifData.device?.firmware || "-"}</div>
                    <div><strong>Серийный номер:</strong> {onvifData.device?.serial_number || "-"}</div>
                  </div>

                  <div style={{ display: "grid", gap: 12 }}>
                    {onvifData.profiles?.map((profile) => (
                      <div
                        key={profile.token}
                        style={{
                          border: "1px solid #e5e7eb",
                          borderRadius: 14,
                          padding: 14,
                          background: "#fff",
                        }}
                      >
                        <div style={{ fontWeight: 700, marginBottom: 8 }}>
                          {profile.name || "Профиль"} {profile.token ? `(${profile.token})` : ""}
                        </div>
                        <div style={{ fontSize: 14, lineHeight: 1.7, marginBottom: 10 }}>
                          <div><strong>Видео:</strong> {profile.video?.codec || "-"}, {profile.video?.width || "-"}x{profile.video?.height || "-"}, fps {profile.video?.fps || "-"}</div>
                          <div><strong>Аудио:</strong> {profile.audio?.codec || "нет"}</div>
                          <div><strong>RTSP (камера):</strong> {profile.raw_stream_uri || "-"}</div>
                          <div><strong>RTSP (для нашей системы):</strong> {profile.stream_uri || "-"}</div>
                        </div>
                        <div className="toolbar">
                          <button className="button secondary small" onClick={() => useProfileAsMain(profile)}>
                            Использовать как Main
                          </button>
                          <button className="button secondary small" onClick={() => useProfileAsSub(profile)}>
                            Использовать как Sub
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>

            {form.protocol === "onvif" ? (
              <div className="card" style={{ marginTop: 18 }}>
                <div className="toolbar" style={{ marginBottom: 12 }}>
                  <button className="button secondary small" onClick={loadOnvifProfileConfig} disabled={onvifConfigBusy}>
                    {onvifConfigBusy ? "Читаем настройки..." : "Загрузить настройки профиля"}
                  </button>
                  <button className="button secondary small" onClick={applyOnvifProfileConfig} disabled={onvifConfigBusy}>
                    {onvifConfigBusy ? "Применяем..." : "Применить настройки"}
                  </button>
                </div>

                <div className="formGrid">
                  <div>
                    <div className="formLabel">Кодек</div>
                    <select className="select" value={onvifConfig.codec} onChange={(e) => patchOnvifConfig("codec", e.target.value)}>
                      <option value="">Не менять</option>
                      <option value="H264">H264</option>
                      <option value="H265">H265</option>
                      <option value="JPEG">JPEG</option>
                    </select>
                  </div>

                  <div>
                    <div className="formLabel">Разрешение</div>
                    <div className="toolbar">
                      <input className="input" placeholder="Ширина" value={onvifConfig.width} onChange={(e) => patchOnvifConfig("width", e.target.value)} />
                      <input className="input" placeholder="Высота" value={onvifConfig.height} onChange={(e) => patchOnvifConfig("height", e.target.value)} />
                    </div>
                  </div>

                  <div>
                    <div className="formLabel">Частота кадров, к/с</div>
                    <input className="input" value={onvifConfig.fps} onChange={(e) => patchOnvifConfig("fps", e.target.value)} />
                  </div>

                  <div>
                    <div className="formLabel">Макс. битрейт</div>
                    <input className="input" value={onvifConfig.bitrate} onChange={(e) => patchOnvifConfig("bitrate", e.target.value)} />
                  </div>

                  <div>
                    <div className="formLabel">Интервал I кадров</div>
                    <input className="input" value={onvifConfig.iframe_interval} onChange={(e) => patchOnvifConfig("iframe_interval", e.target.value)} />
                  </div>

                  <div>
                    <div className="formLabel">Качество</div>
                    <input className="input" value={onvifConfig.quality} onChange={(e) => patchOnvifConfig("quality", e.target.value)} />
                  </div>
                </div>
              </div>
            ) : null}

            <div className="actions">
              <button className="button secondary" onClick={() => setShowEditor(false)}>Отмена</button>
              <button className="button" onClick={saveCamera} disabled={busy}>
                {busy ? "Сохраняем..." : "Сохранить"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {deleteModalOpen ? (
        <div className="modalBackdrop">
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 560 }}>
            <div className="modalHeader">
              <h2 style={{ margin: 0 }}>Удалить камеру</h2>
              <button className="iconCloseButton" onClick={closeDeleteModal} aria-label="Закрыть">×</button>
            </div>
            <p style={{ color: "#475569", marginBottom: 16 }}>
              Камера: <strong>{cameraToDelete?.name}</strong>
            </p>

            <label style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 18 }}>
              <input type="checkbox" checked={deleteFiles} onChange={(e) => setDeleteFiles(e.target.checked)} />
              <span>Удалить также все записи и папку камеры</span>
            </label>

            <div className="badge warn" style={{ marginBottom: 18 }}>
              Если галочка не установлена, камера удалится только из системы, а архив останется на диске.
            </div>

            <div className="actions">
              <button className="button secondary" onClick={closeDeleteModal}>Отмена</button>
              <button className="button secondary" onClick={confirmDeleteCamera} disabled={busy}>
                {busy ? "Удаляем..." : "Удалить"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
      </div>
    </Layout>
  );
}
