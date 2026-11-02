"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../lib/api";
import {
  buildDashboardStatusSummary,
  runtimeStatusUserIdentity,
  shouldStopRuntimeStatusPolling,
  systemHealthIndicatorModel,
  userCanReadRuntimeStatus,
} from "../lib/operatorWarnings";

const REFRESH_MS = 30000;

export function useSystemHealthStatus(currentUser, { enabled = true } = {}) {
  const [runtimeStatus, setRuntimeStatus] = useState(null);
  const [deniedUserIdentity, setDeniedUserIdentity] = useState(null);
  const [loading, setLoading] = useState(false);
  const userIdentity = runtimeStatusUserIdentity(currentUser);
  const canRead = userCanReadRuntimeStatus(currentUser);

  useEffect(() => {
    let cancelled = false;
    let timer = null;
    let refreshTimer = null;
    let inFlight = false;
    let refreshPending = false;
    let denied = false;

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
          setRuntimeStatus(data);
          setDeniedUserIdentity(null);
        }
        return true;
      } catch (error) {
        if (!cancelled) {
          setRuntimeStatus(null);
          if (shouldStopRuntimeStatusPolling(error)) {
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
  }, [canRead, currentUser, enabled, userIdentity]);

  const summary = useMemo(
    () => runtimeStatus ? buildDashboardStatusSummary(runtimeStatus, { limit: 8 }) : null,
    [runtimeStatus]
  );

  const accessDenied = Boolean(canRead && deniedUserIdentity && deniedUserIdentity === userIdentity);
  const model = systemHealthIndicatorModel({
    user: currentUser,
    summary,
    runtimeStatusKnown: runtimeStatus !== null,
    permissionDenied: accessDenied,
  });

  return {
    runtimeStatus,
    summary,
    loading,
    accessDenied,
    ...model,
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
