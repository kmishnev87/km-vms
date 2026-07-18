"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { apiFetch, canAccessPath, clearAuthToken, getAuthToken } from "../lib/api";
import { useCurrentUser } from "../lib/currentUser";
import { normalizeLocale, persistLocale, useI18n } from "../lib/i18n";
import {
  readStorageMigrationActivity,
  STORAGE_MIGRATION_ACTIVITY_EVENT,
  storageMigrationActivitySnapshot,
} from "../lib/storageOperations";
import SystemHealthIndicator from "./SystemHealthIndicator";

const items = [
  { href: "/cameras", labelKey: "nav.cameras", iconSrc: "/assets/icons/ui/camera.png" },
  { href: "/recordings", labelKey: "nav.recordings", iconSrc: "/assets/icons/ui/recordings.png" },
  { href: "/live", labelKey: "nav.live", iconSrc: "/assets/icons/ui/live.png" },
  { href: "/chronology", labelKey: "nav.chronology", iconSrc: "/assets/icons/ui/chronology.png" },
];

function MigrationActivityIndicator({ enabled, onStoragePage, t }) {
  const [activity, setActivity] = useState(null);

  useEffect(() => {
    if (!enabled || !getAuthToken()) {
      setActivity(null);
      return undefined;
    }
    if (onStoragePage) {
      setActivity(readStorageMigrationActivity());
      const handleActivity = (event) => setActivity(event.detail || null);
      window.addEventListener(STORAGE_MIGRATION_ACTIVITY_EVENT, handleActivity);
      return () => window.removeEventListener(STORAGE_MIGRATION_ACTIVITY_EVENT, handleActivity);
    }
    let cancelled = false;
    let polling = false;
    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const result = await apiFetch("/storage/migration/operations/active");
        if (!cancelled) setActivity(storageMigrationActivitySnapshot(result));
      } catch (_) {
        if (!cancelled) setActivity(null);
      } finally {
        polling = false;
      }
    };
    poll();
    const timer = window.setInterval(poll, 4000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [enabled, onStoragePage]);

  if (!enabled) return null;
  const status = String(activity?.status || "unknown");
  const completedBytes = Number(activity?.completedBytes);
  const currentBytes = Number(activity?.currentItemBytes);
  const totalBytes = Number(activity?.totalBytes);
  const percent = Number.isFinite(completedBytes) && Number.isFinite(totalBytes) && totalBytes > 0
    ? Math.min(99, Math.max(0, Math.floor(((completedBytes + (Number.isFinite(currentBytes) ? currentBytes : 0)) / totalBytes) * 100)))
    : null;
  const statusKey = status === "queued"
    ? "nav.migrationQueued"
    : status === "cancel_requested"
      ? "nav.migrationCancelRequested"
      : status === "building"
        ? "nav.migrationPreparing"
        : "nav.migrationRunning";
  const operationId = activity?.operationId;
  const href = operationId ? `/storage?migration=${encodeURIComponent(operationId)}` : "/storage";

  return (
    <span className={`migrationNavIndicatorSlot ${activity ? "isActive" : ""}`} aria-hidden={activity ? undefined : "true"}>
      {activity ? (
        <Link className="migrationNavIndicator" href={href} title={t("nav.migrationOpen")} aria-label={`${t(statusKey)}${percent === null ? "" : `, ${percent}%`}`}>
          <img src="/assets/icons/ui/storage.png" alt="" />
          <span>{t(statusKey)}</span>
          {percent !== null ? <strong>{percent}%</strong> : <i aria-hidden="true" />}
        </Link>
      ) : null}
    </span>
  );
}

export default function Layout({ children }) {
  const pathname = usePathname();
  const router = useRouter();
  const { currentUser, status: currentUserStatus } = useCurrentUser();
  const { t } = useI18n();

  useEffect(() => {
    fetch("/api/system/status")
      .then((response) => response.ok ? response.json() : null)
      .then((status) => {
        if (status?.setup_required) {
          router.replace("/setup");
          return;
        }
        if (status?.language) {
          const normalized = normalizeLocale(status.language);
          persistLocale(normalized);
        }
        if (!getAuthToken()) {
          router.replace("/login");
        }
      })
      .catch(() => {
        if (!getAuthToken()) router.replace("/login");
      });
  }, [router]);

  function logout() {
    clearAuthToken();
    localStorage.removeItem("vms_login_redirect");
    sessionStorage.removeItem("vms_login_redirect");
    router.push("/login");
  }

  const visibleItems = currentUser ? items.filter((item) => canAccessPath(currentUser, item.href)) : [];
  const canOpenStorage = currentUser ? canAccessPath(currentUser, "/storage") : false;
  const canOpenSettings = currentUser ? canAccessPath(currentUser, "/settings") : false;

  useEffect(() => {
    if (pathname === "/login" || pathname === "/setup") return;
    if (!getAuthToken() || currentUserStatus === "no_token" || currentUserStatus === "denied") {
      router.replace("/login");
      return;
    }
    if (!currentUser) return;
    if (pathname !== "/" && !canAccessPath(currentUser, pathname)) {
      router.replace("/live");
    }
  }, [currentUser, currentUserStatus, pathname, router]);

  return (
    <div className="layoutShell">
      <header className="topNav">
        <div className="topNavInner">
          <Link href="/" className="topBrand" aria-label="KM VMS">
            <span>KM</span>
            <span>VMS</span>
          </Link>

          <nav className="topNavItems">
            {visibleItems.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={`topNavItem ${pathname === item.href ? "active" : ""}`}
                title={t(item.labelKey)}
                aria-label={t(item.labelKey)}
              >
                {item.iconSrc ? <img className="topNavIconImage" src={item.iconSrc} alt="" /> : <span className="topNavGlyph">{item.glyph}</span>}
              </Link>
            ))}
          </nav>

          <div className="topNavRight">
            <MigrationActivityIndicator enabled={Boolean(currentUser && canOpenStorage)} onStoragePage={pathname === "/storage"} t={t} />
            {canOpenStorage ? (
              <Link
                href="/storage"
                className={`topNavItem ${pathname === "/storage" ? "active" : ""}`}
                title={t("nav.storage")}
                aria-label={t("nav.storage")}
              >
                <img className="topNavIconImage" src="/assets/icons/ui/storage.png" alt="" />
              </Link>
            ) : null}

            <SystemHealthIndicator
              currentUser={currentUser}
              pathname={pathname}
              label={t("nav.systemHealth")}
              stateLabels={{
                unknown: t("nav.systemHealthUnknown"),
                healthy: t("nav.systemHealthHealthy"),
                problem: t("nav.systemHealthProblem"),
              }}
            />

            {canOpenSettings ? (
              <Link
                href="/settings"
                className={`topNavItem ${pathname === "/settings" ? "active" : ""}`}
                title={t("nav.settings")}
                aria-label={t("nav.settings")}
              >
                <img className="topNavIconImage" src="/assets/icons/ui/settings.png" alt="" />
              </Link>
            ) : null}

            <button
              className="topNavItem topNavButton"
              onClick={logout}
              type="button"
              title={t("common.logout")}
              aria-label={t("common.logout")}
            >
              <img className="topNavIconImage" src="/assets/icons/ui/logout.png" alt="" />
            </button>
          </div>
        </div>
      </header>

      <main className="mainContent">{children}</main>
    </div>
  );
}
