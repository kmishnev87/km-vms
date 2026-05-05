"use client";

import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../lib/api";
import {
  buildOperatorWarnings,
  shouldStopRuntimeStatusPolling,
  userCanReadRuntimeStatus,
} from "../lib/operatorWarnings";

const DEFAULT_REFRESH_MS = 30000;

export default function OperatorProblemBanners({ domains = null, className = "", limit = 6, currentUser = undefined }) {
  const [runtimeStatus, setRuntimeStatus] = useState(null);
  const [accessDenied, setAccessDenied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer = null;

    async function loadStatus() {
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
            if (timer) clearInterval(timer);
          }
        }
        return false;
      }
    }

    async function start() {
      let user = currentUser;
      if (user === undefined) {
        try {
          user = await apiFetch("/auth/me");
        } catch (error) {
          if (!cancelled && shouldStopRuntimeStatusPolling(error)) {
            setAccessDenied(true);
          }
          return;
        }
      }

      if (!userCanReadRuntimeStatus(user)) {
        if (!cancelled) {
          setRuntimeStatus(null);
          setAccessDenied(true);
        }
        return;
      }

      const canContinue = await loadStatus();
      if (!cancelled && canContinue) {
        timer = setInterval(loadStatus, DEFAULT_REFRESH_MS);
      }
    }

    start();
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [currentUser]);

  const warnings = useMemo(
    () => buildOperatorWarnings(runtimeStatus, { domains: domains || undefined, limit }),
    [runtimeStatus, domains, limit]
  );

  if (accessDenied || !warnings.length) return null;

  return (
    <section className={`operatorWarnings ${className}`.trim()} aria-label="Предупреждения оператора">
      {warnings.map((item) => (
        <article key={item.id} className={`operatorWarning operatorWarning-${item.severity}`}>
          <div className="operatorWarningTop">
            <span className="operatorWarningSeverity">{item.severity === "error" ? "Ошибка" : "Предупреждение"}</span>
            <span className="operatorWarningDomain">{item.domain}</span>
          </div>
          <div className="operatorWarningTitle">{item.title}</div>
          <div className="operatorWarningMessage">{item.message}</div>
          {item.affected_count ? (
            <div className="operatorWarningMeta">Затронуто: {item.affected_count}</div>
          ) : null}
          {item.action_hint ? <div className="operatorWarningHint">{item.action_hint}</div> : null}
        </article>
      ))}
    </section>
  );
}
