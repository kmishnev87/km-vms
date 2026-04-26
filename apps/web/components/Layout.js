"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

const items = [
  { href: "/cameras", label: "\u041a\u0430\u043c\u0435\u0440\u044b", iconSrc: "/icons/nav/cameras.png" },
  { href: "/recordings", label: "\u0417\u0430\u043f\u0438\u0441\u0438", iconSrc: "/icons/nav/records.png" },
  { href: "/live", label: "\u041e\u043d\u043b\u0430\u0439\u043d", iconSrc: "/icons/nav/online.png" },
  { href: "/chronology2", label: "\u0425\u0440\u043e\u043d\u043e\u043b\u043e\u0433\u0438\u044f", iconSrc: "/icons/nav/chronology.png" },
];

export default function Layout({ children }) {
  const pathname = usePathname();
  const router = useRouter();

  function logout() {
    localStorage.removeItem("token");
    localStorage.removeItem("vms_login_redirect");
    router.push("/login");
  }

  return (
    <div className="layoutShell">
      <header className="topNav">
        <div className="topNavInner">
          <Link href="/" className="topBrand" aria-label="KM VMS">
            <span>KM</span>
            <span>VMS</span>
          </Link>

          <nav className="topNavItems">
            {items.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={`topNavItem ${pathname === item.href ? "active" : ""}`}
                title={item.label}
                aria-label={item.label}
              >
                <img className="topNavIconImage" src={item.iconSrc} alt="" />
              </Link>
            ))}
          </nav>

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
      </header>

      <main className="mainContent">{children}</main>
    </div>
  );
}
