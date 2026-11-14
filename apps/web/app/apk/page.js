"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import Layout from "../../components/Layout";
import { useI18n } from "../../lib/i18n";
import { fetchAndroidRelease } from "../../lib/androidRelease";

export default function ApkPage() {
  const { t } = useI18n();
  const [state, setState] = useState({ loading: true, release: null, failed: false });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    setState({ loading: true, release: null, failed: false });
    fetchAndroidRelease(controller.signal)
      .then((release) => { if (active) setState({ loading: false, release, failed: false }); })
      .catch(() => { if (active) setState({ loading: false, release: null, failed: true }); })
      .finally(() => clearTimeout(timeout));
    return () => { active = false; clearTimeout(timeout); controller.abort(); };
  }, [attempt]);

  return (
    <Layout>
      <div className="apkPage">
        <section className="apkPanel">
          <div className="apkVisual" aria-hidden="true">
            <img src="/assets/icons/dashboard/apk.png" alt="" />
          </div>
          <div className="apkContent">
            <span aria-live="polite">{t(state.loading ? "androidDownload.checking" : state.release ? "androidDownload.available" : state.failed ? "androidDownload.failed" : "androidDownload.unpublished")}</span>
            <h1>{t("apkPage.title")}</h1>
            <h2>{t("androidDownload.subtitle")}</h2>
            <p>{t("androidDownload.instructions")}</p>
            {state.release && <p>{t("androidDownload.version")} {state.release.version} · {(state.release.bytes / 1048576).toFixed(1)} MB</p>}
            <div className="apkDownloadActions">
              {state.release && <a className="button small" href={state.release.url} rel="noreferrer">{t("androidDownload.download")}</a>}
              <button className="button secondary small" type="button" disabled={state.loading} onClick={() => setAttempt((value) => value + 1)}>{t("androidDownload.check")}</button>
            </div>
            <p>{t("androidDownload.languages")}</p>
            <Link className="button secondary small" href="/">{t("apkPage.back")}</Link>
          </div>
        </section>
      </div>
    </Layout>
  );
}
