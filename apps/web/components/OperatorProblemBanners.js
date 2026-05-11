"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { apiFetch, canAccessPath } from "../lib/api";
import { useCurrentUser } from "../lib/currentUser";
import { useI18n } from "../lib/i18n";
import {
  buildDashboardStatusSummary,
  buildOperatorWarnings,
  shouldStopRuntimeStatusPolling,
  userCanReadRuntimeStatus,
} from "../lib/operatorWarnings";

const DEFAULT_REFRESH_MS = 30000;

function canUseAction(user, action) {
  if (!action?.href) return false;
  return canAccessPath(user, action.href);
}

function ActionLink({ action, currentUser, text }) {
  if (!canUseAction(currentUser, action)) return null;
  return (
    <Link className="operatorWarningAction" href={action.href}>
      {text(action.label)}
    </Link>
  );
}

export default function OperatorProblemBanners({
  domains = null,
  className = "",
  limit = 6,
  currentUser = undefined,
  showOverview = false,
}) {
  const [runtimeStatus, setRuntimeStatus] = useState(null);
  const [accessDenied, setAccessDenied] = useState(false);
  const sharedCurrentUser = useCurrentUser();
  const { text } = useI18n();
  const effectiveCurrentUser = currentUser === undefined ? sharedCurrentUser.currentUser : currentUser;
  const currentUserReady = currentUser !== undefined || !sharedCurrentUser.loading;

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
      if (!currentUserReady) return;

      if (!effectiveCurrentUser) {
        if (!cancelled) {
          setRuntimeStatus(null);
          setAccessDenied(true);
        }
        return;
      }

      if (!userCanReadRuntimeStatus(effectiveCurrentUser)) {
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
  }, [currentUserReady, effectiveCurrentUser]);

  const warnings = useMemo(
    () => buildOperatorWarnings(runtimeStatus, { domains: domains || undefined, limit }),
    [runtimeStatus, domains, limit]
  );
  const overview = useMemo(
    () => showOverview ? buildDashboardStatusSummary(runtimeStatus, { limit }) : null,
    [showOverview, runtimeStatus, limit]
  );

  if (accessDenied) return null;
  if (!showOverview && !warnings.length) return null;

  return (
    <section className={`operatorStatus ${showOverview ? "operatorStatus-dashboard" : ""} ${className}`.trim()} aria-label={text("Предупреждения оператора")}>
      {showOverview && overview ? (
        <article className={`operatorStatusOverview operatorStatusOverview-${overview.severity}`}>
          <div className="operatorStatusOverviewHead">
            <div>
              <div className="operatorStatusEyebrow">{text("Состояние системы")}</div>
              <div className="operatorStatusTitle">{text(overview.title)}</div>
              <div className="operatorStatusText">{text(overview.summary)}</div>
            </div>
            <div className="operatorStatusCounter">
              <strong>{overview.problem_count}</strong>
              <span>{overview.problem_count === 1 ? text("проблема") : text("проблем")}</span>
            </div>
          </div>

          <div className="operatorStatusDomains">
            {overview.rows.map((row) => (
              <div className={`operatorStatusDomain operatorStatusDomain-${row.severity}`} key={row.domain}>
                <span>{text(row.label)}</span>
                <strong>{text(row.severity_label)}</strong>
              </div>
            ))}
          </div>

          {overview.problems.length ? (
            <div className="operatorStatusDrilldown">
              {overview.problems.map((item) => (
                <div className={`operatorStatusProblem operatorStatusProblem-${item.severity}`} key={`overview-${item.id}`}>
                  <div>
                    <span>{text(item.domain_label)}</span>
                    <strong>{text(item.title)}</strong>
                    <small>{item.affected_count ? text("Затронуто: {count}", { count: item.affected_count }) : text(item.message)}</small>
                  </div>
                  <ActionLink action={item.action} currentUser={effectiveCurrentUser} text={text} />
                </div>
              ))}
            </div>
          ) : (
            <div className="operatorStatusQuiet">{text("Нет активных предупреждений. Неактивный онлайн-просмотр считается нейтральным состоянием.")}</div>
          )}

          <div className="operatorStatusHint">{text(overview.diagnostics_hint)}</div>
        </article>
      ) : null}

      {warnings.length ? (
        <div className="operatorWarnings">
          {warnings.map((item) => (
            <article key={item.id} className={`operatorWarning operatorWarning-${item.severity}`}>
              <div className="operatorWarningTop">
                <span className="operatorWarningSeverity">{text(item.severity_label)}</span>
                <span className="operatorWarningDomain">{text(item.domain_label)}</span>
              </div>
              <div className="operatorWarningTitle">{text(item.title)}</div>
              <div className="operatorWarningMessage">{text(item.message)}</div>
              {item.affected_count ? (
                <div className="operatorWarningMeta">{text("Затронуто: {count}", { count: item.affected_count })}</div>
              ) : null}
              {item.action_hint ? <div className="operatorWarningHint">{text(item.action_hint)}</div> : null}
              <ActionLink action={item.action} currentUser={effectiveCurrentUser} text={text} />
            </article>
          ))}
        </div>
      ) : null}
    </section>
  );
}
