"use client";

import Link from "next/link";
import Layout from "../../components/Layout";
import { useI18n } from "../../lib/i18n";

export default function ApkPlaceholderPage() {
  const { t } = useI18n();

  return (
    <Layout>
      <div className="apkPage">
        <section className="apkPanel">
          <div className="apkVisual" aria-hidden="true">
            <img src="/assets/icons/dashboard/apk.png" alt="" />
          </div>
          <div className="apkContent">
            <span>{t("apkPage.status")}</span>
            <h1>{t("apkPage.title")}</h1>
            <h2>{t("apkPage.subtitle")}</h2>
            <p>{t("apkPage.text")}</p>
            <Link className="button secondary small" href="/">{t("apkPage.back")}</Link>
          </div>
        </section>
      </div>
    </Layout>
  );
}
