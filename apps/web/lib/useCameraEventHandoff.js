"use client";
import { useEffect, useRef } from "react";
import { apiFetch, captureAuthSession } from "./api";
import { useAuthSession } from "./useAuthSession";
import { CAMERA_EVENT_OPEN, cameraEventId } from "./cameraEvents";

export default function useCameraEventHandoff({ ready, mode, onTarget, onError }) {
  const session = useAuthSession();
  const callbacks = useRef({ onTarget, onError });
  callbacks.current = { onTarget, onError };
  const handled = useRef("");
  useEffect(() => {
    if (!ready || !session) return undefined;
    const controller = new AbortController();
    const current = captureAuthSession();
    let request = 0;
    async function open(event) {
      if (event?.detail?.mode && event.detail.mode !== mode) return;
      const id = event?.detail?.id || cameraEventId(window.location.search);
      if (!id || !event?.detail?.id && handled.current === `${session}:${id}`) return;
      const key = ++request;
      try {
        const value = await apiFetch(`/camera-events/${encodeURIComponent(id)}/target?mode=${mode}`, { signal: controller.signal });
        if (key !== request || controller.signal.aborted || !current()) return;
        handled.current = `${session}:${id}`;
        callbacks.current.onTarget(value);
      } catch (error) {
        if (!controller.signal.aborted && current() && key === request) callbacks.current.onError(error);
      }
    }
    open();
    window.addEventListener(CAMERA_EVENT_OPEN, open);
    return () => { request++; controller.abort(); window.removeEventListener(CAMERA_EVENT_OPEN, open); };
  }, [ready, mode, session]);
}
