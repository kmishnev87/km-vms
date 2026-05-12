"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../lib/api";
import {
  buildDashboardStatusSummary,
  shouldStopRuntimeStatusPolling,
  userCanReadRuntimeStatus,
} from "../lib/operatorWarnings";

const REFRESH_MS = 30000;

export function useSystemHealthStatus(currentUser, { enabled = true } = {}) {
  const [runtimeStatus, setRuntimeStatus] = useState(null);
  const [accessDenied, setAccessDenied] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer = null;

    async function loadStatus() {
      if (!enabled || !userCanReadRuntimeStatus(currentUser)) {
        if (!cancelled) {
          setRuntimeStatus(null);
          setAccessDenied(!userCanReadRuntimeStatus(currentUser));
          setLoading(false);
        }
        return false;
      }

      setLoading(true);
      try {
        const data = await apiFetch("/system/runtime/status");
        if (!cancelled) {
          setRuntimeStatus(data);
          setAccessDenied(false);
        }
        return true;
      } catch (error) {
        if (!cancelled) {
          setRuntimeStatus(null);
          if (shouldStopRuntimeStatusPolling(error)) {
            setAccessDenied(true);
          }
        }
        return !shouldStopRuntimeStatusPolling(error);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    async function start() {
      const canContinue = await loadStatus();
      if (!cancelled && canContinue) {
        timer = setInterval(loadStatus, REFRESH_MS);
      }
    }

    start();
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [currentUser, enabled]);

  const summary = useMemo(
    () => runtimeStatus ? buildDashboardStatusSummary(runtimeStatus, { limit: 8 }) : null,
    [runtimeStatus]
  );

  return {
    runtimeStatus,
    summary,
    loading,
    accessDenied,
    canRead: userCanReadRuntimeStatus(currentUser),
    hasProblems: Boolean(summary && summary.severity !== "ok"),
  };
}

export default function SystemHealthIndicator({ currentUser, pathname, label }) {
  const status = useSystemHealthStatus(currentUser);

  if (!status.canRead || status.accessDenied) return null;

  const icon = status.hasProblems
    ? "/assets/icons/ui/system-status-alert.png"
    : "/assets/icons/ui/system-status-base.png";

  return (
    <Link
      href="/system-status"
      className={`topNavItem systemHealthNavItem ${pathname === "/system-status" ? "active" : ""} ${status.hasProblems ? "attention" : ""}`}
      title={label}
      aria-label={label}
      data-health-state={status.hasProblems ? "problem" : "normal"}
    >
      <img className="topNavIconImage" src={icon} alt="" />
    </Link>
  );
}
