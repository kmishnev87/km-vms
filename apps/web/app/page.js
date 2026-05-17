"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Layout from "../components/Layout";
import { useSystemHealthStatus } from "../components/SystemHealthIndicator";
import { canAccessPath, getAuthToken } from "../lib/api";
import { useCurrentUser } from "../lib/currentUser";
import { normalizeLocale, persistLocale, useI18n } from "../lib/i18n";

const DASHBOARD_ITEMS = [
  {
    href: "/cameras",
    iconSrc: "/assets/icons/dashboard/camera.png",
    backgroundSrc: "/assets/backgrounds/dashboard-cards/camera.svg",
    titleKey: "dashboard.camerasTitle",
    descriptionKey: "dashboard.camerasText",
  },
  {
    href: "/recordings",
    iconSrc: "/assets/icons/dashboard/recordings.png",
    backgroundSrc: "/assets/backgrounds/dashboard-cards/recordings.svg",
    titleKey: "dashboard.recordingsTitle",
    descriptionKey: "dashboard.recordingsText",
  },
  {
    href: "/live",
    iconSrc: "/assets/icons/dashboard/live.png",
    backgroundSrc: "/assets/backgrounds/dashboard-cards/live.svg",
    titleKey: "dashboard.liveTitle",
    descriptionKey: "dashboard.liveText",
  },
  {
    href: "/chronology",
    iconSrc: "/assets/icons/dashboard/chronology.png",
    backgroundSrc: "/assets/backgrounds/dashboard-cards/chronology.svg",
    titleKey: "dashboard.chronologyTitle",
    descriptionKey: "dashboard.chronologyText",
  },
  {
    href: "/storage",
    iconSrc: "/assets/icons/ui/storage.png",
    backgroundSrc: "/assets/backgrounds/dashboard-cards/storage.svg",
    titleKey: "dashboard.storageTitle",
    descriptionKey: "dashboard.storageText",
  },
  {
    href: "/system-status",
    iconSrc: "/assets/icons/dashboard/system-status-base.png",
    alertIconSrc: "/assets/icons/dashboard/system-status-alert.png",
    backgroundSrc: "/assets/backgrounds/dashboard-cards/system-status.svg",
    titleKey: "dashboard.systemHealthTitle",
    descriptionKey: "dashboard.systemHealthText",
    kind: "systemHealth",
  },
  {
    href: "/apk",
    iconSrc: "/assets/icons/dashboard/apk.png",
    backgroundSrc: "/assets/backgrounds/dashboard-cards/apk.svg",
    titleKey: "dashboard.apkTitle",
    descriptionKey: "dashboard.apkText",
    kind: "placeholder",
  },
  {
    href: "/settings",
    iconSrc: "/assets/icons/dashboard/settings.png",
    backgroundSrc: "/assets/backgrounds/dashboard-cards/settings.svg",
    titleKey: "dashboard.settingsTitle",
    descriptionKey: "dashboard.settingsText",
  },
];

export default function HomePage() {
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const { currentUser, status: currentUserStatus } = useCurrentUser();
  const { t } = useI18n();
  const systemHealth = useSystemHealthStatus(currentUser);

  useEffect(() => {
    fetch("/api/system/status")
      .then((response) => response.ok ? response.json() : null)
      .then((status) => {
        if (status?.setup_required) {
          router.replace("/setup");
          return;
        }
        if (status?.language) {
          persistLocale(normalizeLocale(status.language));
        }
        if (!getAuthToken()) {
          router.replace("/login");
          return;
        }
        setReady(true);
      })
      .catch(() => {
        if (!getAuthToken()) {
          router.replace("/login");
          return;
        }
        setReady(true);
      });
  }, [router]);

  useEffect(() => {
    if (currentUserStatus === "no_token" || currentUserStatus === "denied") {
      router.replace("/login");
    }
  }, [currentUserStatus, router]);

  if (!ready) {
    return (
      <Layout>
        <div className="dashboardPage">
          <section className="dashboardHeader">
            <div>
              <h1 className="dashboardTitle">KM VMS</h1>
              <div className="dashboardSubtitle">{t("common.loading")}</div>
            </div>
          </section>
        </div>
      </Layout>
    );
  }

  const visibleItems = currentUser ? DASHBOARD_ITEMS.filter((item) => canAccessPath(currentUser, item.href)) : [];

  return (
    <Layout>
      <div className="dashboardPage">
        <section className="dashboardHeader">
          <div>
            <h1 className="dashboardTitle">KM VMS</h1>
            <div className="dashboardSubtitle">
              {t("dashboard.subtitle")}
            </div>
          </div>
        </section>

        <section className="dashboardGrid" aria-label={t("dashboard.sections")}>
          {visibleItems.map((item) => {
            const isSystemHealth = item.kind === "systemHealth";
            const iconSrc = isSystemHealth && systemHealth.hasProblems ? item.alertIconSrc : item.iconSrc;
            return (
            <Link
              href={item.href}
              className={`dashboardCard ${item.kind === "placeholder" ? "dashboardCard-placeholder" : ""} ${isSystemHealth && systemHealth.hasProblems ? "dashboardCard-alert" : ""}`}
              key={item.href}
              style={{ "--dashboard-card-bg": `url(${item.backgroundSrc})` }}
            >
              <div className="dashboardCardIcon">
                <img src={iconSrc} alt="" />
              </div>
              <div className="dashboardCardBody">
                <div className="dashboardCardTitle">{t(item.titleKey)}</div>
                <div className="dashboardCardText">{t(item.descriptionKey)}</div>
              </div>
              <div className="dashboardCardArrow">{"\u2192"}</div>
            </Link>
          )})}
        </section>
      </div>
    </Layout>
  );
}
