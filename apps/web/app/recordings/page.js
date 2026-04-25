"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Layout from "../../components/Layout";
import { apiFetch, apiFetchBlob } from "../../lib/api";

const PAGE_SIZE = 30;
const TEXT = {
  title: "\u0417\u0430\u043f\u0438\u0441\u0438",
  subtitle: "\u041f\u0440\u043e\u0441\u043c\u043e\u0442\u0440, \u0441\u043a\u0430\u0447\u0438\u0432\u0430\u043d\u0438\u0435 \u0438 \u0443\u0434\u0430\u043b\u0435\u043d\u0438\u0435 \u0430\u0440\u0445\u0438\u0432\u0430",
  allCameras: "\u0412\u0441\u0435 \u043a\u0430\u043c\u0435\u0440\u044b",
  date: "\u0414\u0430\u0442\u0430",
  refresh: "\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c",
  deleteSelected: "\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u044b\u0435",
  dangerActions: "\u041e\u043f\u0430\u0441\u043d\u044b\u0435 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u044f",
  deleteCamera: "\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0432\u0441\u0435 \u0437\u0430\u043f\u0438\u0441\u0438 \u043a\u0430\u043c\u0435\u0440\u044b",
  deleteAll: "\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0432\u0441\u0435 \u0437\u0430\u043f\u0438\u0441\u0438",
  totalFiles: "\u0412\u0441\u0435\u0433\u043e \u0444\u0430\u0439\u043b\u043e\u0432",
  totalSize: "\u041e\u0431\u0449\u0438\u0439 \u043e\u0431\u044a\u0451\u043c",
  page: "\u0421\u0442\u0440\u0430\u043d\u0438\u0446\u0430",
  selected: "\u0412\u044b\u0431\u0440\u0430\u043d\u043e",
  camera: "\u041a\u0430\u043c\u0435\u0440\u0430",
  file: "\u0424\u0430\u0439\u043b",
  createdAt: "\u0414\u0430\u0442\u0430 \u0441\u043e\u0437\u0434\u0430\u043d\u0438\u044f",
  size: "\u0420\u0430\u0437\u043c\u0435\u0440",
  actions: "\u0414\u0435\u0439\u0441\u0442\u0432\u0438\u044f",
  watch: "\u0421\u043c\u043e\u0442\u0440\u0435\u0442\u044c",
  download: "\u0421\u043a\u0430\u0447\u0430\u0442\u044c",
  remove: "\u0423\u0434\u0430\u043b\u0438\u0442\u044c",
  noRecords: "\u041d\u0435\u0442 \u0437\u0430\u043f\u0438\u0441\u0435\u0439",
  pickAllPage: "\u0412\u044b\u0431\u0440\u0430\u0442\u044c \u0432\u0441\u0435 \u043d\u0430 \u0442\u0435\u043a\u0443\u0449\u0435\u0439 \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0435",
  openEmbeddedViewer: "\u041e\u0442\u043a\u0440\u044b\u0442\u044c \u0432\u0441\u0442\u0440\u043e\u0435\u043d\u043d\u044b\u0439 \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440",
  viewRecord: "\u041f\u0440\u043e\u0441\u043c\u043e\u0442\u0440 \u0437\u0430\u043f\u0438\u0441\u0438",
  close: "\u0417\u0430\u043a\u0440\u044b\u0442\u044c",
};

const ICONS = {
  more: "\u22ef",
  refresh: "\u21bb",
  trash: "\ud83d\uddd1",
  watch: "\u25b6",
  download: "\u2b07",
  remove: "\ud83d\uddd1",
  prev: "\u2190",
  next: "\u2192",
  up: "\u2191",
  down: "\u2193",
  sort: "\u2195",
  gap: "\u2026",
  close: "\u00d7",
};

const SORT_OPTIONS = {
  created_at: { key: "created_at", label: TEXT.createdAt },
  size_bytes: { key: "size_bytes", label: TEXT.size },
  camera: { key: "camera", label: TEXT.camera },
};

function formatSizeBytes(sizeBytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(sizeBytes || 0);
  let unit = units[0];

  for (const currentUnit of units) {
    unit = currentUnit;
    if (value < 1024 || currentUnit === units[units.length - 1]) break;
    value /= 1024;
  }

  if (sizeBytes <= 0) return "0 B";
  if (unit === "GB" || unit === "TB") return `${value.toFixed(2)} ${unit}`;
  if (unit === "MB") return `${value < 100 ? value.toFixed(1) : value.toFixed(0)} ${unit}`;
  if (unit === "KB") return `${value.toFixed(0)} ${unit}`;
  return `${Math.round(value)} ${unit}`;
}

function parseCreatedAt(value) {
  if (!value) return 0;

  const match = String(value).match(
    /^(\d{2})\.(\d{2})\.(\d{4}),\s*(\d{2}):(\d{2}):(\d{2})$/
  );

  if (!match) return 0;

  const [, day, month, year, hours, minutes, seconds] = match;
  return new Date(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hours),
    Number(minutes),
    Number(seconds)
  ).getTime();
}

function formatDateInputFromCreatedAt(value) {
  const ts = parseCreatedAt(value);
  if (!ts) return "";

  const dt = new Date(ts);
  const year = dt.getFullYear();
  const month = String(dt.getMonth() + 1).padStart(2, "0");
  const day = String(dt.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function compareValues(left, right, sortBy, sortDir) {
  let result = 0;

  if (sortBy === SORT_OPTIONS.created_at.key) {
    result = parseCreatedAt(left.created_at) - parseCreatedAt(right.created_at);
  } else if (sortBy === SORT_OPTIONS.size_bytes.key) {
    result = Number(left.size_bytes || 0) - Number(right.size_bytes || 0);
  } else if (sortBy === SORT_OPTIONS.camera.key) {
    result = String(left.camera || "").localeCompare(String(right.camera || ""), "ru", {
      sensitivity: "base",
      numeric: true,
    });
  }

  if (result === 0) {
    result = String(left.filename || "").localeCompare(String(right.filename || ""), "ru", {
      sensitivity: "base",
      numeric: true,
    });
  }

  return sortDir === "asc" ? result : -result;
}

function buildPageList(currentPage, pageCount) {
  if (pageCount <= 1) return [1];
  if (pageCount <= 7) {
    return Array.from({ length: pageCount }, (_, index) => index + 1);
  }

  const pages = new Set([1, pageCount, currentPage - 1, currentPage, currentPage + 1]);
  if (currentPage <= 3) {
    pages.add(2);
    pages.add(3);
    pages.add(4);
  }
  if (currentPage >= pageCount - 2) {
    pages.add(pageCount - 1);
    pages.add(pageCount - 2);
    pages.add(pageCount - 3);
  }

  const ordered = Array.from(pages)
    .filter((page) => page >= 1 && page <= pageCount)
    .sort((left, right) => left - right);

  const result = [];
  ordered.forEach((page, index) => {
    if (index > 0 && page - ordered[index - 1] > 1) {
      result.push("gap");
    }
    result.push(page);
  });

  return result;
}

export default function RecordingsPage() {
  const [cameras, setCameras] = useState([]);
  const [selectedCamera, setSelectedCamera] = useState("__all__");
  const [selectedDate, setSelectedDate] = useState("");
  const [items, setItems] = useState([]);
  const [selectedPaths, setSelectedPaths] = useState([]);
  const [sortBy, setSortBy] = useState(SORT_OPTIONS.created_at.key);
  const [sortDir, setSortDir] = useState("desc");
  const [currentPage, setCurrentPage] = useState(1);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [dangerMenuOpen, setDangerMenuOpen] = useState(false);

  const [viewerOpen, setViewerOpen] = useState(false);
  const [viewerTitle, setViewerTitle] = useState("");
  const [viewerUrl, setViewerUrl] = useState("");

  const requestIdRef = useRef(0);
  const dangerMenuRef = useRef(null);

  async function loadCameras() {
    const data = await apiFetch("/recordings/cameras");
    setCameras(data.items || []);
  }

  async function loadRecordings(camera = "__all__") {
    const requestId = ++requestIdRef.current;
    const query =
      camera && camera !== "__all__"
        ? `?camera=${encodeURIComponent(camera)}`
        : "";

    const data = await apiFetch(`/recordings${query}`);
    if (requestId !== requestIdRef.current) return;

    setItems(data.items || []);
    setSelectedPaths([]);
  }

  async function initialLoad() {
    try {
      setError("");
      await loadCameras();
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

  useEffect(() => {
    setCurrentPage(1);
  }, [selectedCamera, selectedDate, sortBy, sortDir]);

  useEffect(() => {
    function handlePointerDown(event) {
      if (!dangerMenuRef.current?.contains(event.target)) {
        setDangerMenuOpen(false);
      }
    }

    document.addEventListener("mousedown", handlePointerDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
    };
  }, []);

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

  function handleSort(nextSortBy) {
    if (sortBy === nextSortBy) {
      setSortDir((prev) => (prev === "asc" ? "desc" : "asc"));
      return;
    }

    setSortBy(nextSortBy);
    setSortDir(nextSortBy === SORT_OPTIONS.created_at.key ? "desc" : "asc");
  }

  const filteredItems = useMemo(() => {
    return items.filter((item) => {
      if (selectedDate) {
        return formatDateInputFromCreatedAt(item.created_at) === selectedDate;
      }
      return true;
    });
  }, [items, selectedDate]);

  const sortedItems = useMemo(() => {
    return [...filteredItems].sort((left, right) =>
      compareValues(left, right, sortBy, sortDir)
    );
  }, [filteredItems, sortBy, sortDir]);

  const pageCount = Math.max(1, Math.ceil(sortedItems.length / PAGE_SIZE));

  useEffect(() => {
    setCurrentPage((prev) => Math.min(prev, pageCount));
  }, [pageCount]);

  const paginatedItems = useMemo(() => {
    const startIndex = (currentPage - 1) * PAGE_SIZE;
    return sortedItems.slice(startIndex, startIndex + PAGE_SIZE);
  }, [sortedItems, currentPage]);

  const visiblePaths = useMemo(
    () => paginatedItems.map((item) => item.path),
    [paginatedItems]
  );

  const allVisibleSelected = useMemo(() => {
    if (!visiblePaths.length) return false;
    return visiblePaths.every((path) => selectedPaths.includes(path));
  }, [visiblePaths, selectedPaths]);

  const visibleSummary = useMemo(() => {
    const sizeBytes = filteredItems.reduce(
      (total, item) => total + Number(item.size_bytes || 0),
      0
    );

    return {
      count: filteredItems.length,
      size_human: formatSizeBytes(sizeBytes),
    };
  }, [filteredItems]);

  const paginationItems = useMemo(
    () => buildPageList(currentPage, pageCount),
    [currentPage, pageCount]
  );

  function toggleSelectAll() {
    if (allVisibleSelected) {
      setSelectedPaths((prev) => prev.filter((path) => !visiblePaths.includes(path)));
      return;
    }

    setSelectedPaths((prev) => Array.from(new Set([...prev, ...visiblePaths])));
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
    if (!window.confirm(`\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0437\u0430\u043f\u0438\u0441\u044c "${item.filename}"?`)) return;
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
    if (!window.confirm(`\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u044b\u0435 \u0437\u0430\u043f\u0438\u0441\u0438: ${selectedPaths.length} \u0448\u0442.?`)) return;

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
    if (!window.confirm(`\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0432\u0441\u0435 \u0437\u0430\u043f\u0438\u0441\u0438 \u043a\u0430\u043c\u0435\u0440\u044b "${selectedCamera}"?`)) return;

    try {
      setDangerMenuOpen(false);
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
    if (!window.confirm("\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0432\u043e\u043e\u0431\u0449\u0435 \u0432\u0441\u0435 \u0437\u0430\u043f\u0438\u0441\u0438 \u0432\u0441\u0435\u0445 \u043a\u0430\u043c\u0435\u0440?")) return;

    try {
      setDangerMenuOpen(false);
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
      <div className="standardPage">
      <div className="pageHeader recordingsHeader">
        <div>
          <h1 className="pageTitle">{TEXT.title}</h1>
          <div className="pageSubtitle">{TEXT.subtitle}</div>
        </div>
      </div>

      {error ? (
        <div className="badge err recordingsErrorBadge">
          {error}
        </div>
      ) : null}

      <div className="card recordingsFilterCard">
        <div className="recordingsFilterBar">
          <div className="recordingsFilterGroup">
            <select
              className="select recordingsFilterSelect"
              value={selectedCamera}
              onChange={(e) => setSelectedCamera(e.target.value)}
            >
              <option value="__all__">{TEXT.allCameras}</option>
              {cameras.map((camera) => (
                <option key={camera} value={camera}>
                  {camera}
                </option>
              ))}
            </select>

            <input
              type="date"
              className="input recordingsFilterDate"
              value={selectedDate}
              onChange={(e) => setSelectedDate(e.target.value)}
              aria-label={TEXT.date}
            />
          </div>

          <div className="recordingsToolbar recordingsToolbarCompact">
            <button
              className="button secondary small recordingsActionButton recordingsToolbarIconButton"
              onClick={refresh}
              title={TEXT.refresh}
              aria-label={TEXT.refresh}
            >
              {ICONS.refresh}
            </button>

            <button
              className="button secondary small recordingsActionButton recordingsToolbarIconButton"
              onClick={handleDeleteSelected}
              disabled={!selectedPaths.length || busy}
              title={TEXT.deleteSelected}
              aria-label={TEXT.deleteSelected}
            >
              {ICONS.trash}
            </button>

            <div className="recordingsDangerMenu recordingsToolbarMenu" ref={dangerMenuRef}>
              <button
                className="button secondary small recordingsDangerTrigger"
                onClick={() => setDangerMenuOpen((prev) => !prev)}
                aria-haspopup="menu"
                aria-expanded={dangerMenuOpen}
                title={TEXT.dangerActions}
              >
                {ICONS.more}
              </button>

              {dangerMenuOpen ? (
                <div className="recordingsDangerDropdown" role="menu">
                  <div className="recordingsDangerTitle">{TEXT.dangerActions}</div>
                  <button
                    className="recordingsDangerItem"
                    onClick={handleDeleteByCamera}
                    disabled={selectedCamera === "__all__" || busy}
                    role="menuitem"
                  >
                    {TEXT.deleteCamera}
                  </button>
                  <button
                    className="recordingsDangerItem recordingsDangerItemAlert"
                    onClick={handleDeleteAll}
                    disabled={busy}
                    role="menuitem"
                  >
                    {TEXT.deleteAll}
                  </button>
                </div>
              ) : null}
            </div>
          </div>
        </div>
      </div>

      <div className="recordingsStatsRow">
        <div className="badge">{TEXT.totalFiles}: {visibleSummary.count}</div>
        <div className="badge">{TEXT.totalSize}: {visibleSummary.size_human}</div>
        <div className="badge">{TEXT.page}: {currentPage} / {pageCount}</div>
        {selectedPaths.length ? (
          <div className="badge ok">{TEXT.selected}: {selectedPaths.length}</div>
        ) : null}
      </div>

      <div className="card recordingsTableCard">
        <div className="recordingsTableWrap">
          <table className="table recordingsTable">
            <thead>
              <tr>
                <th style={{ width: 44 }}>
                  <input
                    type="checkbox"
                    checked={allVisibleSelected}
                    onChange={toggleSelectAll}
                    aria-label={TEXT.pickAllPage}
                  />
                </th>
                <th>
                  <button
                    className={`recordingsSortButton ${sortBy === SORT_OPTIONS.camera.key ? "active" : ""}`}
                    onClick={() => handleSort(SORT_OPTIONS.camera.key)}
                  >
                    {TEXT.camera}
                    <span>{sortBy === SORT_OPTIONS.camera.key ? (sortDir === "asc" ? ICONS.up : ICONS.down) : ICONS.sort}</span>
                  </button>
                </th>
                <th>{TEXT.file}</th>
                <th>
                  <button
                    className={`recordingsSortButton ${sortBy === SORT_OPTIONS.created_at.key ? "active" : ""}`}
                    onClick={() => handleSort(SORT_OPTIONS.created_at.key)}
                  >
                    {TEXT.createdAt}
                    <span>{sortBy === SORT_OPTIONS.created_at.key ? (sortDir === "asc" ? ICONS.up : ICONS.down) : ICONS.sort}</span>
                  </button>
                </th>
                <th>
                  <button
                    className={`recordingsSortButton ${sortBy === SORT_OPTIONS.size_bytes.key ? "active" : ""}`}
                    onClick={() => handleSort(SORT_OPTIONS.size_bytes.key)}
                  >
                    {TEXT.size}
                    <span>{sortBy === SORT_OPTIONS.size_bytes.key ? (sortDir === "asc" ? ICONS.up : ICONS.down) : ICONS.sort}</span>
                  </button>
                </th>
                <th className="recordingsActionsHeader">{TEXT.actions}</th>
              </tr>
            </thead>
            <tbody>
              {paginatedItems.map((item) => (
                <tr key={item.path}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selectedPaths.includes(item.path)}
                      onChange={() => toggleSelected(item.path)}
                      aria-label={`\u0412\u044b\u0431\u0440\u0430\u0442\u044c ${item.filename}`}
                    />
                  </td>
                  <td className="recordingsCameraCell">{item.camera}</td>
                  <td className="recordingsFilenameCell">
                    <button
                      className="linkButton recordingsFileLink"
                      onClick={() => handleWatch(item)}
                      title={TEXT.openEmbeddedViewer}
                    >
                      {item.filename}
                    </button>
                  </td>
                  <td>{item.created_at || "-"}</td>
                  <td>{item.size_human}</td>
                  <td>
                    <div className="recordingsActions">
                      <button
                        className="recordingsIconButton"
                        onClick={() => handleWatch(item)}
                        title={`${ICONS.watch} ${TEXT.watch}`}
                        aria-label={TEXT.watch}
                      >
                        {ICONS.watch}
                      </button>
                      <button
                        className="recordingsIconButton"
                        onClick={() => handleDownload(item)}
                        title={`${ICONS.download} ${TEXT.download}`}
                        aria-label={TEXT.download}
                      >
                        {ICONS.download}
                      </button>
                      <button
                        className="recordingsIconButton danger"
                        onClick={() => handleDeleteOne(item)}
                        disabled={busy}
                        title={`${ICONS.remove} ${TEXT.remove}`}
                        aria-label={TEXT.remove}
                      >
                        {ICONS.remove}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}

              {!paginatedItems.length ? (
                <tr>
                  <td colSpan="6" className="recordingsEmptyCell">
                    {TEXT.noRecords}
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>

        <div className="recordingsPagination">
          <button
            className="recordingsPageButton"
            onClick={() => setCurrentPage((prev) => Math.max(prev - 1, 1))}
            disabled={currentPage === 1}
          >
            {ICONS.prev}
          </button>

          {paginationItems.map((item, index) =>
            item === "gap" ? (
              <span key={`gap-${index}`} className="recordingsPageGap">
                {ICONS.gap}
              </span>
            ) : (
              <button
                key={item}
                className={`recordingsPageButton ${currentPage === item ? "active" : ""}`}
                onClick={() => setCurrentPage(item)}
              >
                {item}
              </button>
            )
          )}

          <button
            className="recordingsPageButton"
            onClick={() => setCurrentPage((prev) => Math.min(prev + 1, pageCount))}
            disabled={currentPage === pageCount}
          >
            {ICONS.next}
          </button>
        </div>
      </div>

      {viewerOpen ? (
        <div className="modalBackdrop">
          <div className="modal modalWide" onClick={(e) => e.stopPropagation()}>
            <div className="modalHeader">
              <h2 style={{ margin: 0 }}>{TEXT.viewRecord}</h2>
              <button
                className="iconCloseButton"
                onClick={closeViewer}
                aria-label={TEXT.close}
              >
                {ICONS.close}
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
                {TEXT.close}
              </button>
            </div>
          </div>
        </div>
      ) : null}
      </div>
    </Layout>
  );
}
