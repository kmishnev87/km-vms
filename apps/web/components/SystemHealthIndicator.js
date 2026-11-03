"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { apiFetch, getAuthToken } from "../lib/api";
import { runtimeStatusCache } from "../lib/runtimeStatusCache";
import {
  buildDashboardStatusSummary,
  runtimeStatusUserIdentity,
  shouldStopRuntimeStatusPolling,
  systemHealthIndicatorModel,
  userCanReadRuntimeStatus,
} from "../lib/operatorWarnings";

const REFRESH_MS = 30000;

export function useSystemHealthStatus(currentUser, { enabled = true } = {}) {
  const userIdentity = runtimeStatusUserIdentity(currentUser);
  const canRead = userCanReadRuntimeStatus(currentUser);
  const cacheIdentity = currentUser ? JSON.stringify([userIdentity, currentUser.role, [...(currentUser.permissions || [])].sort()]) : null;
  const authSession = getAuthToken();
  const cached = enabled && canRead ? runtimeStatusCache.read(cacheIdentity, authSession) : null;
  const [statusOwner, setStatusOwner] = useState(() => ({ identity: cacheIdentity, session: authSession }));
  const [runtimeStatus, setRuntimeStatus] = useState(() => cached?.runtimeStatus || null);
  const [deniedUserIdentity, setDeniedUserIdentity] = useState(() => cached?.denied ? userIdentity : null);
  const [loading, setLoading] = useState(false);
  const [stale, setStale] = useState(() => Boolean(cached?.stale));

  useEffect(() => {
    const previous = enabled && canRead ? runtimeStatusCache.read(cacheIdentity, authSession) : null;
    setStatusOwner({ identity: cacheIdentity, session: authSession });
    setRuntimeStatus(previous?.runtimeStatus || null);
    setStale(Boolean(previous?.stale));
    setDeniedUserIdentity(previous?.denied ? userIdentity : null);
    let latestRuntimeStatus = previous?.runtimeStatus || null;
    let cancelled = false;
    let timer = null;
    let refreshTimer = null;
    let inFlight = false;
    let refreshPending = false;
    let denied = Boolean(previous?.denied);

    async function loadStatus() {
      if (cancelled || denied) return false;
      if (inFlight) { refreshPending = true; return true; }
      if (!enabled || !canRead) {
        if (!cancelled) {
          setRuntimeStatus(null);
          setLoading(false);
        }
        return false;
      }

      setDeniedUserIdentity((current) => current && current !== userIdentity ? null : current);
      inFlight = true;
      setLoading(true);
      try {
        const data = await apiFetch("/system/runtime/status");
        if (!cancelled) {
          latestRuntimeStatus = data;
          runtimeStatusCache.write(cacheIdentity, authSession, data);
          setRuntimeStatus(data);
          setStale(false);
          setDeniedUserIdentity(null);
        }
        return true;
      } catch (error) {
        if (!cancelled) {
          setStale(true);
          runtimeStatusCache.write(cacheIdentity, authSession, latestRuntimeStatus, { stale: true, denied: shouldStopRuntimeStatusPolling(error) });
          if (shouldStopRuntimeStatusPolling(error)) {
            setRuntimeStatus(null);
            denied = true;
            setDeniedUserIdentity(userIdentity);
            if (timer) clearInterval(timer);
          }
        }
        return !shouldStopRuntimeStatusPolling(error);
      } finally {
        inFlight = false;
        if (!cancelled) setLoading(false);
        if (refreshPending && !cancelled && !denied) {
          refreshPending = false;
          if (refreshTimer) clearTimeout(refreshTimer);
          refreshTimer = setTimeout(loadStatus, 200);
        }
      }
    }

    async function start() {
      const canContinue = await loadStatus();
      if (!cancelled && canContinue) {
        timer = setInterval(loadStatus, REFRESH_MS);
      }
    }

    start();
    const refreshRuntime = () => {
      if (cancelled || denied || !enabled || !canRead) return;
      if (refreshTimer) clearTimeout(refreshTimer);
      refreshTimer = setTimeout(loadStatus, 200);
    };
    window.addEventListener("kmvms:camera-runtime-changed", refreshRuntime);
    window.addEventListener("kmvms:live-runtime-changed", refreshRuntime);
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
      if (refreshTimer) clearTimeout(refreshTimer);
      window.removeEventListener("kmvms:camera-runtime-changed", refreshRuntime);
      window.removeEventListener("kmvms:live-runtime-changed", refreshRuntime);
    };
  }, [canRead, enabled, userIdentity, cacheIdentity, authSession]);

  const ownsStatus = enabled && canRead && statusOwner.identity === cacheIdentity && statusOwner.session === authSession;
  const visibleRuntimeStatus = ownsStatus ? runtimeStatus : null;

  const summary = useMemo(
    () => visibleRuntimeStatus ? buildDashboardStatusSummary(visibleRuntimeStatus, { limit: 8 }) : null,
    [visibleRuntimeStatus]
  );

  const accessDenied = Boolean(ownsStatus && deniedUserIdentity && deniedUserIdentity === userIdentity);
  const model = systemHealthIndicatorModel({
    user: currentUser,
    summary,
    runtimeStatusKnown: visibleRuntimeStatus !== null && !stale,
    permissionDenied: accessDenied,
  });

  return {
    runtimeStatus: visibleRuntimeStatus,
    summary,
    loading,
    accessDenied,
    ...model,
    stale,
    hasProblems: model.hasProblems || Boolean(stale && summary?.problem_count),
  };
}

export default function SystemHealthIndicator({ currentUser, pathname, label, stateLabels = {} }) {
  const status = useSystemHealthStatus(currentUser);

  if (!status.visible) return null;

  const icon = status.hasProblems
    ? "/assets/icons/ui/system-status-alert.png"
    : "/assets/icons/ui/system-status-base.png";
  const stateLabel = stateLabels[status.state] || label;

  return (
    <Link
      href="/system-status"
      className={`topNavItem systemHealthNavItem systemHealthNavItem-${status.state} ${pathname === "/system-status" ? "active" : ""} ${status.hasProblems ? "attention" : ""}`}
      title={stateLabel}
      aria-label={stateLabel}
      aria-busy={status.loading || undefined}
      data-health-state={status.state}
    >
      <img className="topNavIconImage" src={icon} alt="" />
    </Link>
  );
}
