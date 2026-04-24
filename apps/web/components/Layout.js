"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

const items = [
  { href: "/live", label: "\u041e\u043d\u043b\u0430\u0439\u043d", icon: "\ud83d\udcfa" },
  { href: "/recordings", label: "\u0417\u0430\u043f\u0438\u0441\u0438", icon: "\ud83c\udf9e\ufe0f" },
  { href: "/cameras", label: "\u041a\u0430\u043c\u0435\u0440\u044b", icon: "\ud83d\udcf7" },
  { href: "/timeline", label: "\u0425\u0440\u043e\u043d\u043e\u043b\u043e\u0433\u0438\u044f", icon: "\ud83d\udd52" },
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
      <aside className="sidebar">
        <div className="brand">
          <div style={{ lineHeight: "1.12", textAlign: "center" }}>
            <div style={{ fontSize: 17, fontWeight: 900 }}>KM</div>
            <div style={{ fontSize: 17, fontWeight: 900, marginTop: 3 }}>VMS</div>
          </div>
        </div>

        <nav className="sidebarNav">
          {items.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`navItem ${pathname === item.href ? "active" : ""}`}
            >
              <div className="navIcon">{item.icon}</div>
              <div className="navLabel">{item.label}</div>
            </Link>
          ))}

          <button className="navItem navButtonItem" onClick={logout} type="button">
            <div className="navIcon">\u238b</div>
            <div className="navLabel">\u0412\u044b\u0445\u043e\u0434</div>
          </button>
        </nav>
      </aside>

      <main className="mainContent">{children}</main>
    </div>
  );
}
