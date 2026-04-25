"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

const items = [
  { href: "/live2", label: "\u041e\u043d\u043b\u0430\u0439\u043d", icon: "\ud83d\udcfa" },
  { href: "/recordings", label: "\u0417\u0430\u043f\u0438\u0441\u0438", icon: "\ud83c\udf9e\ufe0f" },
  { href: "/cameras", label: "\u041a\u0430\u043c\u0435\u0440\u044b", icon: "\ud83d\udcf7" },
  { href: "/chronology2", label: "\u0425\u0440\u043e\u043d\u043e\u043b\u043e\u0433\u0438\u044f", icon: "\ud83d\udd52" },
];

export default function Layout({ children }) {
  const pathname = usePathname();
  const router = useRouter();

  function logout() {
    localStorage.removeItem("token");
    router.push("/login");
  }

  return (
    <div className="layoutShell">
      <header className="topNav">
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
            >
              <span className="topNavIcon">{item.icon}</span>
              <span className="topNavLabel">{item.label}</span>
            </Link>
          ))}
        </nav>

        <button className="topNavItem topNavButton" onClick={logout} type="button">
          <span className="topNavIcon">{"\u238b"}</span>
          <span className="topNavLabel">Выход</span>
        </button>
      </header>

      <main className="mainContent">{children}</main>
    </div>
  );
}
