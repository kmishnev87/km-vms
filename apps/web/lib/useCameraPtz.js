"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, getAuthToken } from "./api";
import { useCurrentUser } from "./currentUser";
import { useAuthSession } from "./useAuthSession";
import { createPtzController } from "./cameraPtzController";

const capabilityCache = new Map();
let cacheSession = null;
const CACHE_TTL = 120000;
const cameraKey = camera => JSON.stringify([camera.id, camera.protocol, camera.host, camera.port, camera.onvif_profile_token]);

export function invalidateCameraPtz() {
  capabilityCache.clear();
  if (typeof window !== "undefined") window.dispatchEvent(new Event("km-vms-ptz-invalidated"));
}

async function readCapabilities(camera, session) {
  if (cacheSession !== session) { capabilityCache.clear(); cacheSession = session; }
  const key = cameraKey(camera);
  const cached = capabilityCache.get(key);
  if (cached?.pending) return cached.pending;
  if (cached?.expires > Date.now()) return cached.value;
  const entry = { ...cached };
  entry.pending = apiFetch(`/cameras/${camera.id}/onvif/ptz/capabilities`).then(value => {
    // A transport error is not evidence that a known PTZ camera became fixed.
    entry.value = value?.supported == null && cached?.value?.supported ? cached.value : value;
    entry.expires = Date.now() + (value?.supported == null ? 10000 : CACHE_TTL);
    return entry.value;
  }).catch(() => { entry.expires = Date.now() + 10000; return cached?.value || null; })
    .finally(() => { entry.pending = null; });
  capabilityCache.set(key, entry);
  while (capabilityCache.size > 256) capabilityCache.delete(capabilityCache.keys().next().value);
  return entry.pending;
}

export function usePtzCapabilities(cameras) {
  const { currentUser } = useCurrentUser();
  const session = useAuthSession();
  const allowed = Boolean(session && currentUser?.permissions?.includes("manage_cameras"));
  const [values, setValues] = useState({ session: null, byKey: {} });
  const [revision, setRevision] = useState(0);
  const signature = JSON.stringify(cameras.map(c => [c.id, c.protocol, c.host, c.port, c.onvif_profile_token]));
  const cameraRef = useRef(cameras);
  cameraRef.current = cameras;
  useEffect(() => {
    const refresh = () => setRevision(value => value + 1);
    window.addEventListener("focus", refresh);
    window.addEventListener("km-vms-ptz-invalidated", refresh);
    return () => { window.removeEventListener("focus", refresh); window.removeEventListener("km-vms-ptz-invalidated", refresh); };
  }, []);
  useEffect(() => {
    let active = true;
    if (!allowed) { setValues({ session: null, byKey: {} }); return () => { active = false; }; }
    const pending = cameraRef.current.filter(c => !c.protocol || c.protocol === "onvif");
    const ownSession = () => active && getAuthToken() === session;
    async function worker() {
      while (pending.length && ownSession()) {
        const camera = pending.shift();
        const result = await readCapabilities(camera, session);
        if (ownSession()) setValues(previous => ({ session,
          byKey: { ...(previous.session === session ? previous.byKey : {}), [cameraKey(camera)]: result } }));
      }
    }
    void Promise.all(Array.from({ length: Math.min(3, pending.length) }, worker));
    return () => { active = false; };
  }, [signature, allowed, session, revision]);
  return allowed && values.session === session
    ? Object.fromEntries(cameras.map(camera => [camera.id, values.byKey[cameraKey(camera)]])) : {};
}

function newControlId() {
  return globalThis.crypto?.randomUUID?.() || `ptz-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function usePtzController(cameraId, profileToken, onError) {
  const session = useAuthSession();
  const [held, setHeld] = useState(null);
  const mounted = useRef(true);
  const errorRef = useRef(onError);
  errorRef.current = onError;
  const controller = useMemo(() => createPtzController({
    makeId: newControlId,
    isCurrent: () => Boolean(session && getAuthToken() === session),
    send: (payload, options = {}) => apiFetch(`/cameras/${cameraId}/onvif/ptz/command`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...payload, profile_token: profileToken }), ...options,
    }),
    onChange: value => { if (mounted.current) setHeld(value); },
    onError: error => { if (mounted.current) errorRef.current?.(error); },
  }), [cameraId, profileToken, session]);
  useEffect(() => {
    mounted.current = true;
    controller.activate();
    const stop = () => { void controller.stop(); };
    const hidden = () => { if (document.hidden) stop(); };
    const logout = event => event.detail?.waitUntil?.(controller.stop());
    window.addEventListener("blur", stop);
    window.addEventListener("pagehide", stop);
    window.addEventListener("km-vms-before-logout", logout);
    document.addEventListener("visibilitychange", hidden);
    return () => {
      mounted.current = false;
      window.removeEventListener("blur", stop); window.removeEventListener("pagehide", stop);
      window.removeEventListener("km-vms-before-logout", logout); document.removeEventListener("visibilitychange", hidden);
      void controller.dispose();
    };
  }, [controller]);
  return { controller, held };
}
