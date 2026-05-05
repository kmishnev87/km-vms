"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Layout from "../components/Layout";
import OperatorProblemBanners from "../components/OperatorProblemBanners";
import { apiFetch, canAccessPath, getAuthToken } from "../lib/api";

const DASHBOARD_ITEMS = [
  {
    href: "/cameras",
    iconSrc: "/assets/icons/dashboard/camera.png",
    backgroundSrc: "/assets/backgrounds/dashboard-cards/camera.svg",
    title: "Камеры",
    description: "Настройка камер, RTSP-адресов и параметров.",
  },
  {
    href: "/recordings",
    iconSrc: "/assets/icons/dashboard/recordings.png",
    backgroundSrc: "/assets/backgrounds/dashboard-cards/recordings.svg",
    title: "Записи",
    description: "Поиск, просмотр и управление архивными файлами.",
  },
  {
    href: "/live",
    iconSrc: "/assets/icons/dashboard/live.png",
    backgroundSrc: "/assets/backgrounds/dashboard-cards/live.svg",
    title: "Онлайн",
    description: "Живой просмотр камер в свободном workspace.",
  },
  {
    href: "/chronology",
    iconSrc: "/assets/icons/dashboard/chronology.png",
    backgroundSrc: "/assets/backgrounds/dashboard-cards/chronology.svg",
    title: "Хронология",
    description: "Синхронный архив камер с общей временной точкой.",
  },
  {
    href: "/settings",
    iconSrc: "/assets/icons/dashboard/settings.png",
    backgroundSrc: "/assets/backgrounds/dashboard-cards/settings.svg",
    title: {
      ru: "Настройки",
      en: "Settings",
    },
    description: {
      ru: "Конфигурация системы и управление параметрами.",
      en: "System configuration and settings management.",
    },
  },
];

export default function HomePage() {
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const [language, setLanguage] = useState("ru");
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
          return;
        }
        return apiFetch("/auth/me");
      })
      .then((user) => {
        if (user) setCurrentUser(user);
        setReady(true);
      })
      .catch(() => {
        if (!getAuthToken()) {
          router.replace("/login");
          return;
        }
        apiFetch("/auth/me")
          .then((user) => setCurrentUser(user))
          .catch(() => setCurrentUser(null))
          .finally(() => setReady(true));
      });
  }, [router]);

  if (!ready) return null;
  const visibleItems = currentUser ? DASHBOARD_ITEMS.filter((item) => canAccessPath(currentUser, item.href)) : [];

  return (
    <Layout>
      <div className="dashboardPage">
        <section className="dashboardHeader">
          <div>
            <h1 className="dashboardTitle">KM VMS</h1>
            <div className="dashboardSubtitle">
              Рабочий стол системы видеонаблюдения
            </div>
          </div>
        </section>

        <OperatorProblemBanners className="dashboardWarnings" limit={6} currentUser={currentUser} showOverview />

        <section className="dashboardGrid" aria-label="Основные разделы">
          {visibleItems.map((item) => (
            <Link
              href={item.href}
              className="dashboardCard"
              key={item.href}
              style={{ "--dashboard-card-bg": `url(${item.backgroundSrc})` }}
            >
              <div className="dashboardCardIcon">
                <img src={item.iconSrc} alt="" />
              </div>
              <div className="dashboardCardBody">
                <div className="dashboardCardTitle">{typeof item.title === "string" ? item.title : item.title[language] || item.title.ru}</div>
                <div className="dashboardCardText">{typeof item.description === "string" ? item.description : item.description[language] || item.description.ru}</div>
              </div>
              <div className="dashboardCardArrow">{"\u2192"}</div>
            </Link>
          ))}
        </section>
      </div>
    </Layout>
  );
}
