"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
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
const DOMAIN_ORDER = ["storage", "recorder", "cameras", "live", "retention", "reconciliation"];

function severityRank(severity) {
  return severity === "error" ? 2 : severity === "warning" ? 1 : 0;
}

function warningCountLabel(count) {
  if (count === 1) return `${count} предупреждение`;
  if (count > 1 && count < 5) return `${count} предупреждения`;
  return `${count} предупреждений`;
}

function affectedCountLabel(count) {
  return `Затронуто: ${count}`;
}

function displayTitle(item) {
  const reasons = Array.isArray(item?.reason_codes) ? item.reason_codes : [];
  if (item?.domain === "live" && reasons.includes("live_starting")) {
    return "Поток запускается дольше 30 секунд";
  }
  if (item?.domain === "live" && reasons.includes("live_failed")) {
    return "Онлайн-поток не запустился";
  }
  return item?.title || "";
}

function displayMessage(item) {
  const reasons = Array.isArray(item?.reason_codes) ? item.reason_codes : [];
  if (item?.domain === "live" && reasons.includes("live_starting")) {
    return "Запуск онлайн-потока превысил ожидаемое время.";
  }
  if (item?.domain === "live" && reasons.includes("live_failed")) {
    return "Есть явная ошибка запуска онлайн-потока.";
  }
  return item?.message || "";
}

function displayHint(item) {
  const reasons = Array.isArray(item?.reason_codes) ? item.reason_codes : [];
  if (item?.domain === "live" && (reasons.includes("live_starting") || reasons.includes("live_failed"))) {
    return "Откройте диагностику, если проблема сохраняется.";
  }
  return item?.action_hint || "";
}

function routeSection(pathname) {
  const path = String(pathname || "/").split("?")[0].replace(/\/+$/, "") || "/";
  if (path === "/") return "/";
  return `/${path.split("/").filter(Boolean)[0]}`;
}

function actionForContext(action, item, currentPathname) {
  if (!action?.href) return null;
  const currentSection = routeSection(currentPathname);
  const actionSection = routeSection(action.href);
  if (currentSection !== actionSection) return action;
  const reasons = Array.isArray(item?.reason_codes) ? item.reason_codes : [];
  const canUseDiagnostics = (
    item?.domain === "live" &&
    currentSection === "/live" &&
    (item?.severity === "error" || reasons.includes("live_failed") || reasons.includes("live_starting"))
  ) || (
    ["storage", "retention", "reconciliation"].includes(item?.domain) &&
    currentSection === "/storage" &&
    item?.severity === "error"
  );
  if (!canUseDiagnostics) return null;
  return { href: "/diagnostics", label: "Диагностика" };
}

function canUseAction(user, action) {
  if (!action?.href) return false;
  return canAccessPath(user, action.href);
}

function ActionLink({ action, item, currentPathname, currentUser, text }) {
  const effectiveAction = actionForContext(action, item, currentPathname);
  if (!canUseAction(currentUser, effectiveAction)) return null;
  const label = effectiveAction === action ? text(action.label) : text(effectiveAction.label);
  return (
    <Link className="operatorWarningAction" href={effectiveAction.href}>
      {label}
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
  const [detailsOpen, setDetailsOpen] = useState(false);
  const pathname = usePathname();
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
  if (showOverview && !warnings.length) return null;

  const visibleWarnings = showOverview && overview?.problems?.length ? overview.problems : warnings;
  const totalCount = visibleWarnings.length;
  const highestSeverity = visibleWarnings.reduce(
    (current, item) => severityRank(item.severity) > severityRank(current) ? item.severity : current,
    "warning"
  );
  const domainCounts = DOMAIN_ORDER.map((domain) => ({
    domain,
    label: visibleWarnings.find((item) => item.domain === domain)?.domain_label,
    count: visibleWarnings.filter((item) => item.domain === domain).length,
  })).filter((item) => item.count > 0);
  const severityCounts = ["error", "warning"].map((severity) => ({
    severity,
    label: visibleWarnings.find((item) => item.severity === severity)?.severity_label,
    count: visibleWarnings.filter((item) => item.severity === severity).length,
  })).filter((item) => item.count > 0);
  const groupedWarnings = DOMAIN_ORDER.map((domain) => ({
    domain,
    label: visibleWarnings.find((item) => item.domain === domain)?.domain_label,
    items: visibleWarnings.filter((item) => item.domain === domain),
  })).filter((group) => group.items.length > 0);

  return (
    <section className={`operatorStatus operatorStatusCompact ${showOverview ? "operatorStatus-dashboard" : ""} ${className}`.trim()} aria-label={text("Предупреждения оператора")}>
      <div className={`operatorWarningStrip operatorWarningStrip-${highestSeverity}`}>
        <span className="operatorWarningStripIcon" aria-hidden="true">{highestSeverity === "error" ? "!" : "i"}</span>
        <strong className="operatorWarningStripTitle">
          {warningCountLabel(totalCount)}
        </strong>
        <div className="operatorWarningChips" aria-label={text("Группы предупреждений")}>
          {domainCounts.map((item) => (
            <span className="operatorWarningChip" key={item.domain}>{text(item.label)} {item.count}</span>
          ))}
          {severityCounts.map((item) => (
            <span className={`operatorWarningChip operatorWarningChip-${item.severity}`} key={item.severity}>
              {text(item.label)} {item.count}
            </span>
          ))}
        </div>
        <button
          className="operatorWarningToggle"
          type="button"
          aria-expanded={detailsOpen}
          onClick={() => setDetailsOpen((value) => !value)}
        >
          {detailsOpen ? text("Скрыть детали") : text("Показать детали")}
          <span aria-hidden="true">{detailsOpen ? "⌃" : "⌄"}</span>
        </button>
      </div>

      {detailsOpen ? (
        <div className="operatorWarningDetails">
          {groupedWarnings.map((group) => (
            <section className="operatorWarningGroup" key={group.domain} aria-label={text(group.label)}>
              <div className="operatorWarningGroupTitle">{text(group.label)}</div>
              <div className="operatorWarningRows">
                {group.items.map((item) => (
                  <div key={item.id} className={`operatorWarningRow operatorWarningRow-${item.severity}`}>
                    <span className="operatorWarningRowSeverity" aria-label={text(item.severity_label)} title={text(item.severity_label)} />
                    <div className="operatorWarningRowText">
                      <strong>{text(displayTitle(item))}</strong>
                      <span>{item.affected_count ? affectedCountLabel(item.affected_count) : text(displayMessage(item))}</span>
                      {displayHint(item) ? <small>{displayHint(item) === item.action_hint ? text(item.action_hint) : text(displayHint(item))}</small> : null}
                    </div>
                    <ActionLink action={item.action} item={item} currentPathname={pathname} currentUser={effectiveCurrentUser} text={text} />
                  </div>
                ))}
              </div>
            </section>
          ))}
          {overview?.diagnostics_hint ? <div className="operatorWarningDetailsHint">{text(overview.diagnostics_hint)}</div> : null}
        </div>
      ) : null}
    </section>
  );
}
