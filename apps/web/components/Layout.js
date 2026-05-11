"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { apiFetch, canAccessPath, clearAuthToken, getAuthToken } from "../lib/api";
import { useCurrentUser } from "../lib/currentUser";
import { LanguageSelect, normalizeLocale, persistLocale, useI18n } from "../lib/i18n";

const items = [
  { href: "/cameras", labelKey: "nav.cameras", iconSrc: "/assets/icons/ui/camera.png" },
  { href: "/recordings", labelKey: "nav.recordings", iconSrc: "/assets/icons/ui/recordings.png" },
  { href: "/live", labelKey: "nav.live", iconSrc: "/assets/icons/ui/live.png" },
  { href: "/chronology", labelKey: "nav.chronology", iconSrc: "/assets/icons/ui/chronology.png" },
  { href: "/storage", labelKey: "nav.storage", iconSrc: "/assets/icons/ui/storage.png" },
];

export default function Layout({ children }) {
  const pathname = usePathname();
  const router = useRouter();
  const [language, setLanguage] = useState("ru");
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
          setLanguage(normalized);
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

  useEffect(() => {
    function onLanguage(event) {
      if (event.detail) setLanguage(normalizeLocale(event.detail));
    }
    window.addEventListener("km-vms-language", onLanguage);
    return () => window.removeEventListener("km-vms-language", onLanguage);
  }, []);

  async function changeLanguage(nextLanguage) {
    nextLanguage = normalizeLocale(nextLanguage);
    setLanguage(nextLanguage);
    persistLocale(nextLanguage);
    try {
      await apiFetch("/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ language: nextLanguage }),
      });
    } catch (_) {}
  }

  function logout() {
    clearAuthToken();
    localStorage.removeItem("vms_login_redirect");
    sessionStorage.removeItem("vms_login_redirect");
    router.push("/login");
  }

  const username = currentUser?.full_name || currentUser?.username || "";
  const visibleItems = currentUser ? items.filter((item) => canAccessPath(currentUser, item.href)) : [];
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
            <LanguageSelect className="topLanguageSelect" value={language} onChange={changeLanguage} aria-label={t("common.language")} />

            <div className="topUserChip" title={username || t("common.user")}>
              {username || t("common.user")}
            </div>

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
