"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

const items = [
  { href: "/live", label: "Онлайн", icon: "📺" },
  { href: "/recordings", label: "Записи", icon: "🎞️" },
  { href: "/cameras", label: "Камеры", icon: "📷" },
  { href: "/timeline", label: "Хронология", icon: "🕒" },
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
        <div className="brand">VMS</div>

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
        </nav>

        <div className="sidebarSpacer" />

        <button className="button secondary logoutButton" onClick={logout}>
          Выход
        </button>
      </aside>

      <main className="mainContent">{children}</main>
    </div>
  );
}
