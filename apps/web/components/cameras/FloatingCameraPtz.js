"use client";

import { useLayoutEffect, useRef, useState } from "react";
import { CameraPtzBadge, CameraPtzControls } from "./CameraPtz";
import { clampPtzPanel, fullscreenPtzPosition } from "../../lib/ptzUiGeometry";

export default function FloatingCameraPtz({ camera, capability, expanded, onToggle, position: savedPosition,
  onPositionChange, ...controls }) {
  const root = useRef(null), drag = useRef(null), report = useRef(onPositionChange);
  const anchor = useRef(savedPosition || null);
  const [position, setPosition] = useState(savedPosition || null);
  const [defaultInset, setDefaultInset] = useState(null);
  const [dragging, setDragging] = useState(false);
  report.current = onPositionChange;

  function clamped(next) {
    const panel = root.current;
    return panel?.parentElement ? clampPtzPanel(next, panel.parentElement.getBoundingClientRect(), panel.getBoundingClientRect()) : next;
  }
  useLayoutEffect(() => {
    const panel = root.current;
    if (!panel) return;
    anchor.current = savedPosition || null;
    const parent = panel.parentElement;
    const menu = parent.querySelector("[data-live-fullscreen-stream-switch]");
    const check = () => {
      const bounds = parent.getBoundingClientRect();
      const button = menu?.querySelector("button");
      if (button) {
        const menuBounds = menu.getBoundingClientRect(), buttonBounds = button.getBoundingClientRect();
        const next = { right: bounds.right - menuBounds.left + 8, top: buttonBounds.top - bounds.top };
        setDefaultInset(previous => previous?.right === next.right && previous?.top === next.top ? previous : next);
      }
      const next = fullscreenPtzPosition(anchor.current, bounds, panel.getBoundingClientRect(),
        { width: document.documentElement.clientWidth, height: document.documentElement.clientHeight },
        Boolean(document.fullscreenElement?.contains(panel)));
      setPosition(previous => previous?.x === next?.x && previous?.y === next?.y ? previous : next);
    };
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(check) : null;
    observer?.observe(panel); observer?.observe(parent);
    if (menu) observer?.observe(menu);
    document.addEventListener("fullscreenchange", check);
    window.addEventListener("resize", check);
    check();
    return () => {
      observer?.disconnect();
      document.removeEventListener("fullscreenchange", check);
      window.removeEventListener("resize", check);
    };
  }, [savedPosition]);
  function finish(event) {
    const gesture = drag.current;
    if (!gesture || gesture.id !== event.pointerId) return;
    event.preventDefault(); event.stopPropagation();
    drag.current = null; setDragging(false);
    report.current?.(gesture.last);
  }
  return <div ref={root} className={`liveFullscreenPtz ${dragging ? "isDragging" : ""}`}
    data-fullscreen-ptz={camera.id} data-ptz-panel-draggable="true"
    style={position ? { left: position.x, top: position.y, right: "auto" } : defaultInset || undefined}
    onPointerDownCapture={event => {
      if (event.button !== 0 || event.target.closest("button,input,select,textarea,a,.ptzSpeed")) return;
      const panel = root.current, bounds = panel.parentElement.getBoundingClientRect(), rect = panel.getBoundingClientRect();
      event.preventDefault(); event.stopPropagation();
      const start = { x: rect.left - bounds.left, y: rect.top - bounds.top };
      drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, start, last: start };
      setDragging(true); panel.setPointerCapture?.(event.pointerId);
    }}
    onPointerMoveCapture={event => {
      const gesture = drag.current;
      if (!gesture || gesture.id !== event.pointerId) return;
      event.preventDefault(); event.stopPropagation();
      gesture.last = clamped({ x: gesture.start.x + event.clientX - gesture.x, y: gesture.start.y + event.clientY - gesture.y });
      anchor.current = gesture.last;
      setPosition(gesture.last);
    }}
    onPointerUpCapture={finish} onPointerCancelCapture={finish} onLostPointerCapture={finish}
    onDoubleClick={event => event.stopPropagation()}>
    <CameraPtzBadge camera={camera} capability={capability} expanded={expanded} onClick={onToggle} />
    {expanded ? <CameraPtzControls camera={camera} capability={capability} overlay {...controls} /> : null}
  </div>;
}
