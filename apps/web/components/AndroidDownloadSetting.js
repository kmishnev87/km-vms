"use client";

import { useEffect, useState } from "react";
import { useI18n } from "../lib/i18n";
import { fetchAndroidRelease } from "../lib/androidRelease";

export default function AndroidDownloadSetting() {
  const { t } = useI18n();
  const [state, setState] = useState({ loading: true, release: null, failed: false });
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
  }, []);

  const status = state.release ? `${t("androidDownload.version")} ${state.release.version}` :
    t(state.loading ? "androidDownload.checking" : state.failed ? "androidDownload.failed" : "androidDownload.unpublished");
  return (
    <div className="settingsRow settingsAndroidRow" data-testid="settings-android-download">
      <div className="settingsRowIcon"><img src="/assets/icons/dashboard/apk.png" alt="" /></div>
      <div className="settingsRowText">
        <strong>{t("dashboard.apkTitle")}</strong>
        <span aria-live="polite" title={status}>{status}</span>
      </div>
      <div className="settingsRowControl settingsRowControlMeta">
        {state.release ? <a className="button secondary small appIllustratedAction settingsUsersAddButton" href={state.release.url}
          rel="noreferrer" title={t("androidDownload.download")} aria-label={t("androidDownload.download")}>
          <img src="/assets/icons/ui/download-report.svg" alt="" aria-hidden="true" />
        </a> : <button className="button secondary small appIllustratedAction settingsUsersAddButton" type="button" disabled
          title={t("androidDownload.download")} aria-label={t("androidDownload.download")}>
          <img src="/assets/icons/ui/download-report.svg" alt="" aria-hidden="true" />
        </button>}
      </div>
    </div>
  );
}
