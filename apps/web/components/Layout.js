"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { apiFetch, canAccessPath, clearAuthToken, getAuthToken } from "../lib/api";

const items = [
  { href: "/cameras", label: "\u041a\u0430\u043c\u0435\u0440\u044b", iconSrc: "/icons/nav/cameras.png" },
  { href: "/recordings", label: "\u0417\u0430\u043f\u0438\u0441\u0438", iconSrc: "/icons/nav/records.png" },
  { href: "/live", label: "\u041e\u043d\u043b\u0430\u0439\u043d", iconSrc: "/icons/nav/online.png" },
  { href: "/chronology", label: "\u0425\u0440\u043e\u043d\u043e\u043b\u043e\u0433\u0438\u044f", iconSrc: "/icons/nav/chronology.png" },
];

export default function Layout({ children }) {
  const pathname = usePathname();
  const router = useRouter();
  const [language, setLanguage] = useState("ru");
  const [username, setUsername] = useState("");
  const [currentUser, setCurrentUser] = useState(null);

  useEffect(() => {
    fetch("/api/system/status")
      .then((response) => response.ok ? response.json() : null)
      .then((status) => {
        if (status?.setup_required) {
          router.replace("/setup");
          return;
        }
        if (status?.language) {
          setLanguage(status.language);
          localStorage.setItem("km_vms_language", status.language);
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
    if (!getAuthToken()) return;
    apiFetch("/auth/me")
      .then((user) => {
        setCurrentUser(user);
        setUsername(user?.full_name || user?.username || "");
      })
      .catch(() => {
        setCurrentUser(null);
        setUsername("");
      });
  }, []);

  useEffect(() => {
    function onLanguage(event) {
      if (event.detail) setLanguage(event.detail);
    }
    window.addEventListener("km-vms-language", onLanguage);
    return () => window.removeEventListener("km-vms-language", onLanguage);
  }, []);

  async function changeLanguage(event) {
    const nextLanguage = event.target.value;
    setLanguage(nextLanguage);
    localStorage.setItem("km_vms_language", nextLanguage);
    window.dispatchEvent(new CustomEvent("km-vms-language", { detail: nextLanguage }));
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

  const visibleItems = currentUser ? items.filter((item) => canAccessPath(currentUser, item.href)) : [];
  const canOpenSettings = currentUser ? canAccessPath(currentUser, "/settings") : false;

  useEffect(() => {
    if (!currentUser || pathname === "/login" || pathname === "/setup") return;
    if (pathname !== "/" && !canAccessPath(currentUser, pathname)) {
      router.replace("/live");
    }
  }, [currentUser, pathname, router]);

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
                title={item.label}
                aria-label={item.label}
              >
                {item.iconSrc ? <img className="topNavIconImage" src={item.iconSrc} alt="" /> : <span className="topNavGlyph">{item.glyph}</span>}
              </Link>
            ))}
          </nav>

          <div className="topNavRight">
            <select className="topLanguageSelect" value={language} onChange={changeLanguage} aria-label="Language">
              <option value="ru">RU</option>
              <option value="en">EN</option>
            </select>

            <div className="topUserChip" title={username || (language === "en" ? "User" : "Пользователь")}>
              {username || (language === "en" ? "User" : "Пользователь")}
            </div>

            {canOpenSettings ? (
              <Link
                href="/settings"
                className={`topNavItem ${pathname === "/settings" ? "active" : ""}`}
                title={"\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438"}
                aria-label={"\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438"}
              >
                <img className="topNavIconImage" src="/icons/nav/settings-icon.png" alt="" />
              </Link>
            ) : null}

            <button
              className="topNavItem topNavButton"
              onClick={logout}
              type="button"
              title={"\u0412\u044b\u0445\u043e\u0434"}
              aria-label={"\u0412\u044b\u0445\u043e\u0434"}
            >
              <img className="topNavIconImage" src="/icons/nav/logout.png" alt="" />
            </button>
          </div>
        </div>
      </header>

      <main className="mainContent">{children}</main>
    </div>
  );
}
