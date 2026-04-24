"use client";

import { useEffect, useMemo, useState } from "react";
import Layout from "../../components/Layout";
import { apiFetch, apiFetchBlob } from "../../lib/api";

export default function RecordingsPage() {
  const [cameras, setCameras] = useState([]);
  const [selectedCamera, setSelectedCamera] = useState("__all__");
  const [items, setItems] = useState([]);
  const [summary, setSummary] = useState({ count: 0, size_human: "0 B" });
  const [selectedPaths, setSelectedPaths] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const [viewerOpen, setViewerOpen] = useState(false);
  const [viewerTitle, setViewerTitle] = useState("");
  const [viewerUrl, setViewerUrl] = useState("");

  async function loadCameras() {
    const data = await apiFetch("/recordings/cameras");
    setCameras(data.items || []);
  }

  async function loadRecordings(camera = "__all__") {
    const query =
      camera && camera !== "__all__"
        ? `?camera=${encodeURIComponent(camera)}`
        : "";
    const data = await apiFetch(`/recordings${query}`);
    setItems(data.items || []);
    setSummary(data.summary || { count: 0, size_human: "0 B" });
    setSelectedPaths([]);
  }

  async function initialLoad() {
    try {
      setError("");
      await Promise.all([loadCameras(), loadRecordings("__all__")]);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    initialLoad();
  }, []);

  useEffect(() => {
    loadRecordings(selectedCamera).catch((err) => setError(err.message));
  }, [selectedCamera]);

  async function refresh() {
    try {
      setError("");
      await Promise.all([loadCameras(), loadRecordings(selectedCamera)]);
    } catch (err) {
      setError(err.message);
    }
  }

  function toggleSelected(path) {
    setSelectedPaths((prev) =>
      prev.includes(path) ? prev.filter((x) => x !== path) : [...prev, path]
    );
  }

  const allVisibleSelected = useMemo(() => {
    if (!items.length) return false;
    return items.every((item) => selectedPaths.includes(item.path));
  }, [items, selectedPaths]);

  function toggleSelectAll() {
    if (allVisibleSelected) {
      setSelectedPaths([]);
      return;
    }
    setSelectedPaths(items.map((item) => item.path));
  }

  async function handleDownload(item) {
    try {
      setError("");
      const { blob, filename } = await apiFetchBlob(
        `/recordings/download?path=${encodeURIComponent(item.path)}`
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename || item.filename || "recording.mp4";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err.message);
    }
  }

  function handleWatch(item) {
    try {
      setError("");
      const url = `/api/recordings/stream?path=${encodeURIComponent(item.path)}`;
      setViewerTitle(item.filename);
      setViewerUrl(url);
      setViewerOpen(true);
    } catch (err) {
      setError(err.message);
    }
  }

  function closeViewer() {
    setViewerTitle("");
    setViewerUrl("");
    setViewerOpen(false);
  }

  async function handleDeleteOne(item) {
    if (!window.confirm(`Удалить запись "${item.filename}"?`)) return;
    try {
      setError("");
      setBusy(true);
      await apiFetch(`/recordings?path=${encodeURIComponent(item.path)}`, {
        method: "DELETE",
      });
      await refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteSelected() {
    if (!selectedPaths.length) return;
    if (!window.confirm(`Удалить выбранные записи: ${selectedPaths.length} шт.?`)) return;

    try {
      setError("");
      setBusy(true);
      await apiFetch("/recordings/bulk-delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths: selectedPaths }),
      });
      await refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteByCamera() {
    if (!selectedCamera || selectedCamera === "__all__") return;
    if (!window.confirm(`Удалить все записи камеры "${selectedCamera}"?`)) return;

    try {
      setError("");
      setBusy(true);
      await apiFetch(`/recordings/by-camera?camera=${encodeURIComponent(selectedCamera)}`, {
        method: "DELETE",
      });
      await refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteAll() {
    if (!window.confirm("Удалить вообще все записи всех камер?")) return;

    try {
      setError("");
      setBusy(true);
      await apiFetch("/recordings/all", { method: "DELETE" });
      await refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Layout>
      <div className="pageHeader">
        <div>
          <h1 className="pageTitle">Записи</h1>
          <div className="pageSubtitle">Просмотр, скачивание и удаление архива</div>
        </div>
      </div>

      {error ? (
        <div className="badge err" style={{ marginBottom: 14 }}>
          {error}
        </div>
      ) : null}

      <div className="card" style={{ marginBottom: 18 }}>
        <div className="toolbar recordingsToolbar">
          <select
            className="select"
            style={{ minWidth: 220 }}
            value={selectedCamera}
            onChange={(e) => setSelectedCamera(e.target.value)}
          >
            <option value="__all__">Все камеры</option>
            {cameras.map((camera) => (
              <option key={camera} value={camera}>
                {camera}
              </option>
            ))}
          </select>

          <button className="button secondary small" onClick={refresh}>
            Обновить
          </button>

          <button
            className="button secondary small"
            onClick={handleDeleteSelected}
            disabled={!selectedPaths.length || busy}
          >
            Удалить выбранные
          </button>

          <button
            className="button secondary small"
            onClick={handleDeleteByCamera}
            disabled={selectedCamera === "__all__" || busy}
          >
            Удалить все записи камеры
          </button>

          <button
            className="button secondary small"
            onClick={handleDeleteAll}
            disabled={busy}
          >
            Удалить все записи
          </button>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 18 }}>
        <div className="toolbar">
          <div className="badge">Всего файлов: {summary?.count || 0}</div>
          <div className="badge">Общий объём: {summary?.size_human || "0 B"}</div>
          {selectedPaths.length ? (
            <div className="badge ok">Выбрано: {selectedPaths.length}</div>
          ) : null}
        </div>
      </div>

      <div className="card">
        <table className="table recordingsTable">
          <thead>
            <tr>
              <th style={{ width: 40 }}>
                <input
                  type="checkbox"
                  checked={allVisibleSelected}
                  onChange={toggleSelectAll}
                />
              </th>
              <th>Камера</th>
              <th>Файл</th>
              <th>Дата создания</th>
              <th>Размер</th>
              <th>Действия</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.path}>
                <td>
                  <input
                    type="checkbox"
                    checked={selectedPaths.includes(item.path)}
                    onChange={() => toggleSelected(item.path)}
                  />
                </td>
                <td>{item.camera}</td>
                <td>
                  <button
                    className="linkButton"
                    onClick={() => handleWatch(item)}
                    title="Открыть встроенный просмотр"
                  >
                    {item.filename}
                  </button>
                </td>
                <td>{item.created_at || "-"}</td>
                <td>{item.size_human}</td>
                <td>
                  <div className="toolbar recordingsActions">
                    <button
                      className="button secondary small"
                      onClick={() => handleWatch(item)}
                    >
                      Смотреть
                    </button>
                    <button
                      className="button secondary small"
                      onClick={() => handleDownload(item)}
                    >
                      Скачать
                    </button>
                    <button
                      className="button secondary small"
                      onClick={() => handleDeleteOne(item)}
                      disabled={busy}
                    >
                      Удалить
                    </button>
                  </div>
                </td>
              </tr>
            ))}

            {!items.length ? (
              <tr>
                <td colSpan="6">Записей пока нет.</td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      {viewerOpen ? (
        <div className="modalBackdrop">
          <div className="modal modalWide" onClick={(e) => e.stopPropagation()}>
            <div className="modalHeader">
              <h2 style={{ margin: 0 }}>Просмотр записи</h2>
              <button
                className="iconCloseButton"
                onClick={closeViewer}
                aria-label="Закрыть"
              >
                ×
              </button>
            </div>

            <div style={{ marginBottom: 14, color: "#475569" }}>{viewerTitle}</div>

            <video
              key={viewerUrl}
              src={viewerUrl}
              controls
              autoPlay
              preload="metadata"
              className="recordingVideo"
            />

            <div className="actions">
              <button className="button secondary" onClick={closeViewer}>
                Закрыть
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </Layout>
  );
}
