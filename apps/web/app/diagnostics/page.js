"use client";

import Layout from "../../components/Layout";
import { DiagnosticsEntry } from "../../components/AuditDiagnosticsEntries";
import { useI18n } from "../../lib/i18n";

export default function DiagnosticsPage() {
  const { t } = useI18n();

  return (
    <Layout>
      <div className="settingsPage">
        <div className="settingsWorkspace">
          <div className="pageHeader settingsHeader">
            <div className="settingsTitleBlock">
              <img src="/assets/icons/ui/diagnostics.svg" alt="" />
              <div>
                <h1 className="pageTitle">{t("diagnosticsEntry.title")}</h1>
                <p className="pageSubtitle">{t("diagnosticsEntry.subtitle")}</p>
              </div>
            </div>
          </div>
          <div className="settingsSecurityModal">
            <DiagnosticsEntry />
          </div>
        </div>
      </div>
    </Layout>
  );
}
