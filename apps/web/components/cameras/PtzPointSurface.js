"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ptzImageBounds, ptzImagePoint, ptzImageRectangle } from "../../lib/ptzUiGeometry";
import { useI18n } from "../../lib/i18n";

export default function PtzPointSurface({ dimensions, onAim, onAreaZoom, onCancel, ready, mode = "point" }) {
  const { t } = useI18n();
  const pointer = useRef(null), pending = useRef(false), alive = useRef(true), markerTimer = useRef(null);
  const cancelRef = useRef(onCancel);
  cancelRef.current = onCancel;
  const [marker, setMarker] = useState(null), [selection, setSelection] = useState(null);
  const [busy, setBusy] = useState(false);
  const area = mode === "area";
  useEffect(() => {
    alive.current = true;
    const cancel = event => {
      if (event.key !== "Escape") return;
      pointer.current = null; setSelection(null); setMarker(null);
      void cancelRef.current?.();
    };
    window.addEventListener("keydown", cancel);
    return () => {
      alive.current = false; pointer.current = null; clearTimeout(markerTimer.current);
      window.removeEventListener("keydown", cancel);
      void cancelRef.current?.();
    };
  }, []);
  // Complete the mode/video reset before the browser can deliver a new gesture.
  // A passive effect could otherwise clear a pointer that has just been pressed.
  useLayoutEffect(() => {
    pointer.current = null; setSelection(null); setMarker(null);
    if (!ready) void cancelRef.current?.();
  }, [ready, mode, dimensions.width, dimensions.height]);
  const stop = event => { event.preventDefault(); event.stopPropagation(); };
  const sameBounds = (a, b) => ["left", "top", "width", "height"].every(key => Math.abs(a[key] - b[key]) < .5);
  function rectangleStyle(rectangle, bounds) {
    const image = ptzImageBounds(bounds, dimensions);
    return image && rectangle ? { left: image.left - bounds.left + rectangle.left * image.width,
      top: image.top - bounds.top + rectangle.top * image.height,
      width: (rectangle.right - rectangle.left) * image.width,
      height: (rectangle.bottom - rectangle.top) * image.height } : null;
  }
  function cancelPointer(event) {
    stop(event);
    // Normal pointerup releases capture too; it must not cancel its own command.
    if (pointer.current?.id !== event.pointerId) return;
    pointer.current = null; setSelection(null);
  }
  async function finish(event) {
    stop(event);
    const down = pointer.current;
    pointer.current = null; setSelection(null);
    if (!ready || pending.current || !down || down.id !== event.pointerId) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    if (!sameBounds(down.bounds, bounds)) return;
    const target = area ? ptzImageRectangle(down, { x: event.clientX, y: event.clientY }, bounds, dimensions)
      : Math.hypot(event.clientX - down.x, event.clientY - down.y) <= 8
        ? ptzImagePoint(event.clientX, event.clientY, bounds, dimensions) : null;
    if (!target) return;
    pending.current = true; setBusy(true);
    setMarker(area ? { area: true, style: rectangleStyle(target, bounds) }
      : { x: event.clientX - bounds.left, y: event.clientY - bounds.top });
    clearTimeout(markerTimer.current);
    try {
      const accepted = await (area ? onAreaZoom?.(target) : onAim?.(target));
      if (accepted === false && alive.current) setMarker(null);
    }
    finally {
      pending.current = false;
      if (alive.current) {
        setBusy(false);
        markerTimer.current = setTimeout(() => setMarker(null), 600);
      }
    }
  }
  return <div className={`ptzPointSurface ${busy ? "isBusy" : ""}`} data-ptz-point-surface="true"
    data-ptz-area-surface={area ? "true" : undefined}
    aria-label={t(area ? "ptz.areaMode" : "ptz.pointMode")} aria-busy={busy}
    title={t(area ? "ptz.areaHint" : "ptz.pointHint")}
    onPointerDown={event => {
      stop(event);
      if (event.isPrimary === false) { pointer.current = null; setSelection(null); return; }
      if (!ready || pending.current || event.button !== 0) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      if (!ptzImagePoint(event.clientX, event.clientY, bounds, dimensions)) return;
      pointer.current = { id: event.pointerId, x: event.clientX, y: event.clientY, bounds };
      setMarker(null);
      event.currentTarget.setPointerCapture?.(event.pointerId);
    }}
    onPointerMove={event => {
      const down = pointer.current;
      if (!area || !down || down.id !== event.pointerId) return;
      stop(event);
      const bounds = event.currentTarget.getBoundingClientRect();
      if (!sameBounds(down.bounds, bounds)) { pointer.current = null; setSelection(null); return; }
      setSelection(rectangleStyle(ptzImageRectangle(down, { x: event.clientX, y: event.clientY }, bounds, dimensions), bounds));
    }}
    onPointerUp={finish} onPointerCancel={cancelPointer} onLostPointerCapture={cancelPointer}
    onClick={stop} onDoubleClick={stop} onDragStart={stop}>
    {selection ? <span className="ptzAreaMarker" style={selection} aria-hidden="true" /> : null}
    {marker ? marker.area ? <span className="ptzAreaMarker" style={marker.style} aria-hidden="true" />
      : <span className="ptzPointMarker" style={{ left: marker.x, top: marker.y }} aria-hidden="true" /> : null}
  </div>;
}
