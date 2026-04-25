"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Layout from "../components/Layout";

const DASHBOARD_ITEMS = [
  {
    href: "/live2",
    icon: "\ud83d\udcfa",
    title: "\u041e\u043d\u043b\u0430\u0439\u043d",
    description: "\u0416\u0438\u0432\u043e\u0439 \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440 \u043a\u0430\u043c\u0435\u0440 \u0432 \u0441\u0432\u043e\u0431\u043e\u0434\u043d\u043e\u043c workspace.",
  },
  {
    href: "/recordings",
    icon: "\ud83c\udf9e\ufe0f",
    title: "\u0417\u0430\u043f\u0438\u0441\u0438",
    description: "\u041f\u043e\u0438\u0441\u043a, \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440 \u0438 \u0443\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u0430\u0440\u0445\u0438\u0432\u043d\u044b\u043c\u0438 \u0444\u0430\u0439\u043b\u0430\u043c\u0438.",
  },
  {
    href: "/cameras",
    icon: "\ud83d\udcf7",
    title: "\u041a\u0430\u043c\u0435\u0440\u044b",
    description: "\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0430 \u043a\u0430\u043c\u0435\u0440, RTSP-\u0430\u0434\u0440\u0435\u0441\u043e\u0432 \u0438 \u043f\u0430\u0440\u0430\u043c\u0435\u0442\u0440\u043e\u0432.",
  },
  {
    href: "/chronology2",
    icon: "\ud83d\udd52",
    title: "\u0425\u0440\u043e\u043d\u043e\u043b\u043e\u0433\u0438\u044f",
    description: "\u0421\u0438\u043d\u0445\u0440\u043e\u043d\u043d\u044b\u0439 \u0430\u0440\u0445\u0438\u0432 \u043a\u0430\u043c\u0435\u0440 \u0441 \u043e\u0431\u0449\u0435\u0439 \u0432\u0440\u0435\u043c\u0435\u043d\u043d\u043e\u0439 \u0442\u043e\u0447\u043a\u043e\u0439.",
  },
];

export default function HomePage() {
  const router = useRouter();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const token = localStorage.getItem("token");
    if (!token) {
      router.replace("/login");
      return;
    }
    setReady(true);
  }, [router]);

  if (!ready) return null;

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

        <section className="dashboardGrid" aria-label="Основные разделы">
          {DASHBOARD_ITEMS.map((item) => (
            <Link href={item.href} className="dashboardCard" key={item.href}>
              <div className="dashboardCardIcon">{item.icon}</div>
              <div className="dashboardCardBody">
                <div className="dashboardCardTitle">{item.title}</div>
                <div className="dashboardCardText">{item.description}</div>
              </div>
              <div className="dashboardCardArrow">{"\u2192"}</div>
            </Link>
          ))}
        </section>
      </div>
    </Layout>
  );
}
