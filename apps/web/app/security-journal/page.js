"use client";

import { Suspense } from "react";
import Layout from "../../components/Layout";
import { SecurityJournalEntry } from "../../components/AuditDiagnosticsEntries";
import { useI18n } from "../../lib/i18n";

export default function SecurityJournalPage() {
  const { t } = useI18n();

  return (
    <Layout>
      <div className="settingsPage">
        <div className="settingsWorkspace">
          <div className="pageHeader settingsHeader">
            <div className="settingsTitleBlock">
              <img src="/assets/icons/ui/security.png" alt="" />
              <div>
                <h1 className="pageTitle">{t("securityJournal.title")}</h1>
                <p className="pageSubtitle">{t("securityJournal.subtitle")}</p>
              </div>
            </div>
          </div>
          <div className="settingsSecurityModal">
            <Suspense fallback={<div className="settingsJournalEmpty">{t("common.loading")}</div>}>
              <SecurityJournalEntry />
            </Suspense>
          </div>
        </div>
      </div>
    </Layout>
  );
}
