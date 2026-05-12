"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Layout from "../../components/Layout";
import { useSystemHealthStatus } from "../../components/SystemHealthIndicator";
import { canAccessPath, getAuthToken } from "../../lib/api";
import { useCurrentUser } from "../../lib/currentUser";
import { useI18n } from "../../lib/i18n";

const DOMAIN_ORDER = ["cameras", "recorder", "live", "storage", "retention", "reconciliation"];

function safeSeverity(value) {
  return ["ok", "warning", "error"].includes(value) ? value : "unknown";
}

function domainMeta(row, t) {
  if (row.domain === "live" && row.severity === "ok") return t("systemStatus.neutralLive");
  if (row.problem_count > 0) return t("systemStatus.affected", { count: row.affected_count || row.problem_count });
  return t("systemStatus.domainOk");
}

function problemCountLabel(count, t) {
  return count === 1 ? t("systemStatus.problemCountOne") : t("systemStatus.problemCount");
}

const ACTION_ICON_BY_HREF = {
  "/cameras": "/assets/icons/ui/camera.png",
  "/diagnostics": "/assets/icons/ui/diagnostics.svg",
  "/live": "/assets/icons/ui/live.png",
  "/storage": "/assets/icons/ui/storage.png",
};

function canUseAction(user, action) {
  if (!action?.href) return false;
  return canAccessPath(user, action.href);
}

function SystemStatusProblemAction({ action, currentUser, text }) {
  if (!canUseAction(currentUser, action)) return null;
  const icon = ACTION_ICON_BY_HREF[action.href] || "/assets/icons/ui/system-status-base.png";
  const label = text(action.label);
  return (
    <Link className="systemStatusIconAction" href={action.href} title={label} aria-label={label}>
      <img src={icon} alt="" />
    </Link>
  );
}

export default function SystemStatusPage() {
  const router = useRouter();
  const { currentUser, status: currentUserStatus, loading } = useCurrentUser();
  const { t, text } = useI18n();
  const systemHealth = useSystemHealthStatus(currentUser, { enabled: Boolean(currentUser) });
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!getAuthToken() || currentUserStatus === "no_token" || currentUserStatus === "denied") {
      router.replace("/login");
      return;
    }
    if (!loading) setReady(true);
  }, [currentUserStatus, loading, router]);

  if (!ready) return null;

  const canRead = systemHealth.canRead && !systemHealth.accessDenied;
  const summary = systemHealth.summary;
  const severity = safeSeverity(summary?.severity || "unknown");
  const hasProblems = severity === "warning" || severity === "error";
  const problemCount = summary?.problem_count || 0;
  const rows = DOMAIN_ORDER.map((domain) => {
    const row = summary?.rows?.find((item) => item.domain === domain);
    return {
      domain,
      severity: safeSeverity(row?.severity || (summary ? "ok" : "unknown")),
      problem_count: row?.problem_count || 0,
      affected_count: row?.affected_count || 0,
    };
  });
  const problems = summary?.problems || [];

  return (
    <Layout>
      <div className="systemStatusPage">
        {!canRead ? (
          <section className="systemStatusAccessState">
            <img src="/assets/icons/ui/system-status-base.png" alt="" />
            <div>
              <h1>{t("systemStatus.forbidden")}</h1>
              <p>{t("systemStatus.forbiddenText")}</p>
            </div>
          </section>
        ) : (
          <section className="systemStatusPanel">
            <header className="systemStatusHead">
              <img
                src={hasProblems ? "/assets/icons/dashboard/system-status-alert.png" : "/assets/icons/dashboard/system-status-base.png"}
                alt=""
              />
              <div>
                <div className="systemStatusEyebrow">{t("systemStatus.eyebrow")}</div>
                <h1>{summary ? (hasProblems ? t("systemStatus.problemTitle") : t("systemStatus.okTitle")) : t("systemStatus.loading")}</h1>
                <p>{summary ? (hasProblems ? t("systemStatus.problemSummary") : t("systemStatus.okSummary")) : t("systemStatus.loading")}</p>
              </div>
              <div className={`systemStatusScore ${hasProblems ? "problem" : "ok"}`}>
                <strong>{problemCount}</strong>
                <span>{problemCountLabel(problemCount, t)}</span>
              </div>
            </header>

            <div className="systemStatusBody">
              <aside className="systemStatusRail">
                {rows.map((row) => (
                  <div className={`systemStatusDomain ${row.severity}`} key={row.domain}>
                    <span className={`systemStatusDot ${row.severity}`} />
                    <div>
                      <div className="systemStatusDomainName">{t(`systemStatus.domains.${row.domain}`)}</div>
                      <div className="systemStatusDomainMeta">{domainMeta(row, t)}</div>
                    </div>
                    <span className={`systemStatusBadge ${row.severity}`}>{t(`systemStatus.status.${row.severity}`)}</span>
                  </div>
                ))}
              </aside>

              <section className="systemStatusDetail">
                <div className="systemStatusDetailTop">
                  <div>
                    <h2>{t("systemStatus.activeProblems")}</h2>
                    <p>{t("systemStatus.safeHint")}</p>
                  </div>
                  <span>{t("systemStatus.updated")}: {systemHealth.runtimeStatus?.generated_at || "-"}</span>
                </div>

                <div className="systemStatusIncidents">
                  {problems.length ? problems.map((item) => {
                    const itemSeverity = safeSeverity(item.severity);
                    const domain = t(`systemStatus.domains.${item.domain}`);
                    return (
                      <article className={`systemStatusIncident ${itemSeverity}`} key={item.id}>
                        <div className="systemStatusIncidentIcon">{itemSeverity === "error" ? "!" : "i"}</div>
                        <div>
                          <h3>{text(item.title)}</h3>
                          <p>{text(item.message)}</p>
                          <span>{t("systemStatus.incidentDomain", { domain })}</span>
                        </div>
                        <div className="systemStatusActions" aria-label={t("systemStatus.activeProblems")}>
                          <SystemStatusProblemAction action={item.action} currentUser={currentUser} text={text} />
                        </div>
                      </article>
                    );
                  }) : (
                    <article className="systemStatusIncident ok">
                      <div className="systemStatusIncidentIcon">0</div>
                      <div>
                        <h3>{t("systemStatus.okTitle")}</h3>
                        <p>{t("systemStatus.noProblems")}</p>
                        <span>{t("systemStatus.incidentDomain", { domain: t("systemStatus.title") })}</span>
                      </div>
                      <div className="systemStatusActions" aria-label={t("systemStatus.title")}>
                        <Link className="systemStatusIconAction" href="/settings" title={t("nav.settings")}>
                          <img src="/assets/icons/ui/settings.png" alt="" />
                        </Link>
                      </div>
                    </article>
                  )}
                </div>

                <div className="systemStatusHint">{t("systemStatus.safeHint")}</div>
              </section>
            </div>
          </section>
        )}
      </div>
    </Layout>
  );
}
