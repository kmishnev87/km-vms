"use client";

import { OperationDialog, OperationRefreshButton } from "./OperationFeedback";
import { updateApplyFactRows } from "../lib/settingsUpdateApplyHelpers";
import {
  formatMaintenanceMessage,
  maintenanceStatusClass,
  maintenanceStatusText,
} from "../lib/settingsPageHelpers";

function MaintenanceCheckIcon() {
  return <span aria-hidden="true" className="storageOpsCheckIcon">✓</span>;
}
function MaintenanceBackupDimensionStatus({ tone, label }) {
  if (!["ok", "problem"].includes(tone)) {
    return <strong className={`settingsMaintenanceBackupStatusPill is-${tone}`}>{label}</strong>;
  }
  return (
    <strong
      className={`settingsMaintenanceBackupStatusPill is-${tone} is-symbol`}
      role="img"
      aria-label={label}
      title={label}
    >
      {tone === "ok"
        ? <MaintenanceCheckIcon />
        : <span aria-hidden="true" className="storageOpsCheckIcon">×</span>}
    </strong>
  );
}

function MaintenanceTrashIcon() {
  return (
    <svg className="recordingsUiIcon recordingsTrashIcon recordingsRowSvgIcon storageOpsTrashIcon" viewBox="0 1 24 24" aria-hidden="true" focusable="false">
      <path d="M4.2 6.8h15.6"></path>
      <path d="M8.9 6.8V4.5h6.2v2.3"></path>
      <path d="M6.7 7.2 7.6 19c.1 1.05 1 1.9 2.05 1.9h4.7c1.05 0 1.95-.85 2.05-1.9l.9-11.8"></path>
      <path d="M10.1 10.7v6.6"></path>
      <path d="M13.9 10.7v6.6"></path>
    </svg>
  );
}

function MaintenanceRestoreIcon() {
  return (
    <svg className="settingsMaintenanceRestoreIcon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <ellipse cx="12" cy="5.5" rx="6.2" ry="2.6" />
      <path d="M5.8 5.5v5.2c0 1.4 2.8 2.6 6.2 2.6 1.1 0 2.1-.1 3-.3" />
      <path d="M5.8 10.7v5.2c0 1.4 2.8 2.6 6.2 2.6" />
      <path d="M19 13.3a4.2 4.2 0 1 0 .5 4.7" />
      <path d="M19.1 10.9v3.5h-3.5" />
    </svg>
  );
}

function currentRestoreDialogContent(controller, t) {
  const {
    CURRENT_RESTORE_CONFIRMATION_PHRASE,
    CURRENT_RESTORE_OPERATIONAL_PHASES,
    currentRestoreDialog,
    currentRestoreStatus,
    currentRestoreFailedPhase,
    currentRestoreTerminal,
    currentRestoreTerminalText,
    updateCurrentRestorePhrase,
  } = controller;
  if (!currentRestoreDialog) return null;
  const status = currentRestoreStatus;
  const terminal = currentRestoreTerminal(status);
  const phase = String(status?.phase || "preflight");
  const phaseOrder = CURRENT_RESTORE_OPERATIONAL_PHASES;
  const activeIndex = phaseOrder.indexOf(phase);
  const failedPhase = terminal
    ? currentRestoreFailedPhase(status)
    : "";
  const failedIndex = phaseOrder.indexOf(failedPhase);
  const terminalLabel = terminal
    ? t.maintenanceCurrentRestorePhases?.[status.terminal_result]
    : t.maintenanceCurrentRestoreTerminalPhase;
  return (
    <div className="settingsCurrentRestoreContent">
      {currentRestoreDialog.preflightBusy ? (
        <div className="settingsCurrentRestorePreflight" role="status">
          <span aria-hidden="true" />
          {t.maintenanceCurrentRestorePreflight}
        </div>
      ) : null}
      {currentRestoreDialog.accepted ? (
        <ol className="settingsCurrentRestoreTimeline">
          {phaseOrder.map((item, index) => {
            const state = terminal
              ? status?.terminal_result === "completed"
                ? "complete"
                : status?.terminal_result === "failed_rolled_back"
                  ? index === failedIndex
                    ? "failed"
                    : "complete"
                  : failedIndex < 0
                    ? "pending"
                    : index < failedIndex
                      ? "complete"
                      : index === failedIndex
                        ? "failed"
                        : "pending"
              : index < activeIndex
                ? "complete"
                : index === activeIndex
                  ? "active"
                  : "pending";
            return (
              <li className={`is-${state}`} key={item}>
                <span aria-hidden="true">
                  {state === "complete"
                    ? "✓"
                    : state === "failed"
                      ? "!"
                      : index + 1}
                </span>
                {t.maintenanceCurrentRestorePhases?.[item]}
              </li>
            );
          })}
          {(() => {
            const resultState = !terminal
              ? "pending"
              : status?.terminal_result === "completed"
                ? "complete"
                : status?.terminal_result === "failed_rolled_back"
                  ? "rolled-back"
                  : "failed";
            return (
              <li className={`is-${resultState}`}>
                <span aria-hidden="true">
                  {resultState === "complete"
                    ? "✓"
                    : resultState === "rolled-back"
                      ? "↩"
                      : resultState === "failed"
                        ? "!"
                        : "7"}
                </span>
                {terminalLabel}
              </li>
            );
          })()}
        </ol>
      ) : (
        <label className="settingsCurrentRestorePhrase">
          <span>{t.maintenanceCurrentRestorePhraseLabel}</span>
          <input
            className="input"
            value={currentRestoreDialog.phrase}
            onChange={(event) => updateCurrentRestorePhrase(event.target.value)}
            autoComplete="off"
            spellCheck="false"
            disabled={currentRestoreDialog.preflightBusy}
          />
          {currentRestoreDialog.phrase
            && currentRestoreDialog.phrase !== CURRENT_RESTORE_CONFIRMATION_PHRASE
            ? <small>{t.maintenanceCurrentRestorePhraseMismatch}</small>
            : null}
        </label>
      )}
      {currentRestoreDialog.reconnecting && !terminal ? (
        <div className="settingsCurrentRestoreReconnect">{t.maintenanceCurrentRestoreReconnect}</div>
      ) : null}
      {terminal ? (
        <div
          className={`settingsCurrentRestoreTerminal is-${status.terminal_result}`}
          role={status.terminal_result === "completed" ? "status" : "alert"}
        >
          {currentRestoreTerminalText(status)}
        </div>
      ) : null}
      {currentRestoreDialog.error ? (
        <div className="settingsCurrentRestoreError" role="alert">
          {currentRestoreDialog.error}
        </div>
      ) : null}
    </div>
  );
}

export function SettingsMaintenanceConfirmationDialogs({ controller, t }) {
  const {
    CURRENT_RESTORE_CONFIRMATION_PHRASE,
    maintenanceConfirm,
    maintenanceBusy,
    closeMaintenanceConfirmation,
    currentRestoreDialog,
    currentRestoreStatus,
    currentRestoreTerminal,
    confirmCurrentDatabaseRestore,
    closeCurrentRestoreDialog,
  } = controller;

  return (
    <>
      <OperationDialog
        dialog={maintenanceConfirm ? {
          id: "maintenance-confirm",
          presentation: "compact-confirmation",
          title: maintenanceConfirm.title,
          message: maintenanceConfirm.text,
          busy: Boolean(maintenanceBusy),
          dismissible: !maintenanceBusy,
          cancelLabel: t.cancel,
          closeLabel: t.close,
          confirmLabel: maintenanceBusy === "backup-delete"
            ? t.maintenanceBackupDeleting
            : maintenanceBusy === "backup-check"
              ? t.maintenanceBackupChecking
              : maintenanceBusy === "backup-create"
                ? t.maintenanceBackupCreating
                : maintenanceBusy === "db-adoption-apply"
                  ? t.saving
                  : maintenanceConfirm.confirmLabel,
          confirmTone: maintenanceConfirm.danger ? "danger" : undefined,
          tone: maintenanceConfirm.danger ? "error" : "warning",
          onConfirm: maintenanceConfirm.onConfirm,
        } : null}
        onClose={closeMaintenanceConfirmation}
      />

      <OperationDialog
        dialog={currentRestoreDialog ? {
          id: `current-db-restore-${currentRestoreDialog.artifact?.id || "current"}`,
          title: t.maintenanceCurrentRestoreTitle,
          message: t.maintenanceCurrentRestoreIntro.replace(
            "{date}",
            currentRestoreDialog.artifact?.createdAt || "-",
          ),
          overlayClassName: "settingsCurrentRestoreDialogOverlay",
          className: "settingsCurrentRestoreDialog",
          tone: currentRestoreStatus?.terminal_result === "completed"
            ? "success"
            : currentRestoreStatus?.terminal_result === "failed_recovery_required"
              ? "error"
              : "warning",
          items: [
            t.maintenanceCurrentRestoreChanges,
            t.maintenanceCurrentRestoreVideoSafe,
            t.maintenanceCurrentRestoreBackupFirst,
            t.maintenanceCurrentRestoreInterruption,
            t.maintenanceCurrentRestoreActor,
          ],
          content: currentRestoreDialogContent(controller, t),
          busy: Boolean(currentRestoreDialog.accepted && !currentRestoreTerminal()),
          dismissible: !currentRestoreDialog.accepted || currentRestoreTerminal(),
          closeLabel: t.maintenanceCurrentRestoreClose,
          ...(!currentRestoreDialog.accepted ? {
            cancelLabel: t.maintenanceCurrentRestoreCancel,
            confirmLabel: t.maintenanceCurrentRestoreConfirm,
            confirmTone: "danger",
            confirmDisabled: Boolean(
              currentRestoreDialog.preflightBusy
              || currentRestoreDialog.preflight?.can_restore !== true
              || currentRestoreDialog.phrase !== CURRENT_RESTORE_CONFIRMATION_PHRASE
            ),
            onConfirm: confirmCurrentDatabaseRestore,
          } : {}),
        } : null}
        onClose={closeCurrentRestoreDialog}
      />
    </>
  );
}

export function SettingsMaintenanceModal({
  controller,
  t,
  lang,
  securityBusy,
  onOpenDiagnosticChoice,
}) {
  const {
    maintenanceModalOpen,
    maintenanceDialogRef,
    maintenanceChildDialogOpen,
    maintenanceLoading,
    maintenanceBusy,
    maintenanceError,
    maintenanceOverview,
    maintenanceBackupDetail,
    maintenanceBackupDetailOpen,
    maintenanceActionResult,
    maintenanceBackupResult,
    maintenanceBackupPending,
    currentRestorePending,
    updateStatus,
    updateApplyStatus,
    updateApplyOperator,
    updateApplyHasUnknownLaunch,
    updateApplyRunning,
    updateApplyAllowed,
    updateApplyPrimaryText,
    updateApplyErrors,
    updatePeerCheckUnavailable,
    maintenanceBackupOverview,
    maintenanceBackupManager,
    maintenanceDatabase,
    maintenanceWarnings,
    maintenanceOverall,
    maintenanceBackupResultModel,
    maintenanceBackupProgressText,
    updateApplyLaunchNotice,
    refreshMaintenanceSurface,
    closeMaintenanceModal,
    runUpdateCheck,
    startUpdateApply,
    runMaintenanceDryRun,
    requestDbAdoptionApply,
    createMaintenanceBackup,
    openMaintenanceBackupDetail,
    closeMaintenanceBackupDetail,
    currentRestoreReasonText,
    requestCurrentDatabaseRestore,
    requestCheckMaintenanceBackup,
    requestDeleteMaintenanceBackup,
    loadMaintenanceBackupPage,
  } = controller;

  const { maintenanceDetail, setMaintenanceDetail } = controller;
  const updateDetails = <>
    <p role="status">{updateApplyOperator.summary}</p>
    <dl className="maintenanceDetailFacts">{updateApplyFactRows(updateStatus, updateApplyStatus, t).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    <div className="settingsUpdateApplyTimeline" aria-label={t.updateApplyProgress}><ol>{updateApplyOperator.timeline.map((step) => <li className={`is-${step.status}`} key={step.name}>
      <span className="settingsUpdateApplyTimelineDot" aria-hidden="true">{step.icon === "alert" ? "!" : step.icon === "check" ? "✓" : "•"}</span><strong>{step.label}</strong><small>{step.timeLabel || "\u00a0"}</small>
    </li>)}</ol></div>
    {updateApplyOperator.releaseSummary ? <p>{updateApplyOperator.releaseSummary}</p> : null}
    {updateApplyOperator.releaseChangelog.length ? <ul>{updateApplyOperator.releaseChangelog.map((item, index) => <li key={index}>{item}</li>)}</ul> : null}
    {updateApplyLaunchNotice ? <p>{updateApplyLaunchNotice}</p> : null}
    {updatePeerCheckUnavailable ? <p>{t.updateApplyPeerCheckUnavailable}</p> : null}
    {updateApplyErrors.map((message) => <p role="alert" key={message}>{message}</p>)}
  </>;
  const actionDetails = maintenanceActionResult ? <>
    <strong>{t.maintenanceFlows?.[maintenanceActionResult.flowKey] || maintenanceActionResult.flowKey}: {maintenanceStatusText(maintenanceActionResult.status, t)}</strong>
    <p>{maintenanceActionResult.displayReason || formatMaintenanceMessage(maintenanceActionResult.reason, t, lang, "action")}</p>
  </> : <p>{maintenanceDatabase.summary}</p>;

  return (
    <>
      {maintenanceModalOpen ? (
        <div className="settingsModalOverlay" role="presentation">
          <div
            ref={maintenanceDialogRef}
            className="settingsMaintenanceModal"
            role="dialog"
            tabIndex={-1}
            aria-modal="true"
            aria-label={t.maintenanceOverview}
            aria-hidden={maintenanceChildDialogOpen ? "true" : undefined}
            inert={maintenanceChildDialogOpen ? true : undefined}
          >
            <div className="settingsMaintenanceModalHeader">
              <h2>{t.maintenanceOverview}</h2>
              <div className="settingsMaintenanceModalActions">
                <OperationRefreshButton busy={maintenanceLoading || Boolean(maintenanceBusy)} error={maintenanceError} label={t.maintenanceRefresh} onClick={refreshMaintenanceSurface} />
                <button type="button" className="settingsMaintenanceIconButton" onClick={closeMaintenanceModal} disabled={maintenanceChildDialogOpen} aria-label={t.close}>×</button>
              </div>
            </div>

            {!maintenanceOverview && !updateStatus && !updateApplyStatus ? <div className="settingsMaintenanceContent" role="status">{maintenanceError || t.checking}</div> : null}

            {maintenanceOverview || updateStatus || updateApplyStatus ? (
              <div className="settingsMaintenanceContent">
                {!maintenanceBackupDetailOpen ? (
                  <>
                <section className={`settingsMaintenanceOverall is-${maintenanceOverall.tone}`}>
                  <span className="settingsMaintenanceOverallIcon" aria-hidden="true">{maintenanceOverall.icon}</span>
                  <div>
                    <strong>{maintenanceOverall.title}</strong>
                    <p>{maintenanceOverall.summary}</p>
                  </div>
                </section>

                <section className="settingsUpdateApplyPanel">
                  <div className="settingsMaintenanceCardHeading settingsMaintenanceCardHeadingAligned">
                    <h3>{t.updateApplyTitle}</h3>
                    <button type="button" className={`settingsMaintenancePill is-${updateApplyOperator.severity}`} onClick={() => setMaintenanceDetail("update")} title={`${t.open}: ${t.updateApplyTitle}`}>{updateApplyOperator.headline}{updateApplyOperator.availableVersion ? ` ${updateApplyOperator.availableVersion}` : ""}</button>
                  </div>
                  <div className="settingsUpdateApplyCompact">
                    <div className="settingsUpdateApplyRelease">
                      <dl>
                        <div>
                          <dt>{t.updateApplyInstalledVersion}</dt>
                          <dd>{updateApplyOperator.currentVersion}</dd>
                        </div>
                        <div>
                          <dt>{t.maintenanceLastUpdate}</dt>
                          <dd>{updateApplyOperator.installedAt}</dd>
                        </div>
                      </dl>
                      <p>{updateApplyOperator.releaseTitle}</p>
                    </div>
                    <div className="settingsUpdateApplyActions">
                      <button
                        type="button"
                        className="button secondary small appIllustratedAction settingsMaintenanceActionIcon"
                        onClick={runUpdateCheck}
                        disabled={Boolean(maintenanceBusy)}
                        title={maintenanceBusy === "update" ? t.checking : t.updateApplyCheck}
                        aria-label={maintenanceBusy === "update" ? t.checking : t.updateApplyCheck}
                        aria-busy={maintenanceBusy === "update" ? "true" : undefined}
                      >
                        <img src="/assets/icons/ui/update-check.svg" alt="" aria-hidden="true" />
                      </button>
                      {updateApplyOperator.showApplyButton ? (
                        <button type="button" className="button primary small" onClick={startUpdateApply} disabled={!updateApplyAllowed}>
                          {maintenanceBusy === "update-apply" || updateApplyRunning || updateApplyHasUnknownLaunch || updateApplyOperator.stateUnknown
                            ? updateApplyPrimaryText
                            : t.updateApplyStart}
                        </button>
                      ) : null}
                    </div>
                  </div>

                </section>

                <div className="settingsMaintenanceCoreGrid">
                  <section className={`settingsMaintenanceCoreCard is-${maintenanceDatabase.tone}`}>
                    <div className="settingsMaintenanceCardHeading">
                      <h3>{t.maintenanceDatabaseTitle}</h3>
                      <button type="button" className={`settingsMaintenancePill is-${maintenanceDatabase.tone}`} onClick={() => setMaintenanceDetail("result")} title={`${t.open}: ${t.maintenanceDatabaseTitle}`}>
                        {maintenanceDatabase.statusLabel}
                      </button>
                    </div>
                    {maintenanceDatabase.facts.length ? (
                      <dl className="settingsMaintenanceCoreFacts">
                        {maintenanceDatabase.facts.map(([label, value]) => (
                          <div key={label}>
                            <dt>{label}</dt>
                            <dd>{String(value)}</dd>
                          </div>
                        ))}
                      </dl>
                    ) : null}
                    <p>{maintenanceDatabase.summary}</p>
                    {maintenanceDatabase.tone !== "ok" && maintenanceDatabase.action ? (
                      <small>{maintenanceDatabase.action}</small>
                    ) : null}
                    {maintenanceDatabase.actionableRow?.showCheck ? (
                      <button
                        type="button"
                        className="button secondary small settingsMaintenanceCoreAction"
                        onClick={() => runMaintenanceDryRun(maintenanceDatabase.actionableRow.key)}
                        disabled={Boolean(maintenanceBusy)}
                      >
                        {maintenanceBusy === maintenanceDatabase.actionableRow.key
                          ? t.checking
                          : maintenanceDatabase.actionableRow.checkLabel}
                      </button>
                    ) : null}
                    {maintenanceDatabase.actionableRow?.showApply ? (
                      <button
                        type="button"
                        className="button primary small settingsMaintenanceCoreAction"
                        onClick={requestDbAdoptionApply}
                        disabled={Boolean(maintenanceBusy) || Boolean(currentRestorePending)}
                      >
                        {maintenanceBusy === "db-adoption-apply"
                          ? t.saving
                          : maintenanceDatabase.actionableRow.applyLabel}
                      </button>
                    ) : null}
                  </section>

                <section className={`settingsMaintenanceBackupManager settingsMaintenanceCoreCard is-${maintenanceBackupOverview.tone}`}>
                  <div className="settingsMaintenanceBackupHead">
                    <div className="settingsMaintenanceCardHeading settingsMaintenanceCardHeadingAligned">
                      <h3>{t.maintenanceBackupsTitle}</h3>
                      <button type="button" className={`settingsMaintenancePill is-${maintenanceBackupOverview.tone}`} onClick={() => setMaintenanceDetail("backup")} title={`${t.open}: ${t.maintenanceBackupsTitle}`}>
                        {maintenanceBackupPending || maintenanceBackupResult?.recovering ? t.checking : maintenanceBackupOverview.statusText}
                      </button>
                    </div>
                  </div>
                  <p className="settingsMaintenanceBackupTotals">
                    {maintenanceBackupOverview.countText} · {maintenanceBackupOverview.totalBytesText}
                  </p>
                  <div className="settingsMaintenanceBackupLatestRow">
                    <div className="settingsMaintenanceBackupLatest">
                      <span>{t.maintenanceBackupLatest}: {maintenanceBackupOverview.latestCreatedAt}</span>
                      {maintenanceBackupOverview.latestArtifact ? (
                        <span>
                          {maintenanceBackupOverview.latestArtifact.availabilityLabel}
                          {" · "}
                          {maintenanceBackupOverview.latestArtifact.integrityLabel}
                          {" · "}
                          {maintenanceBackupOverview.latestArtifact.compatibilityLabel}
                        </span>
                      ) : (
                        <span>{t.maintenanceBackupNoCopies}</span>
                      )}
                    </div>
                    <div className="settingsMaintenanceBackupActions">
                      <button
                        type="button"
                        className="button secondary small appIllustratedAction settingsMaintenanceActionIcon"
                        onClick={createMaintenanceBackup}
                        disabled={Boolean(maintenanceBusy) || Boolean(maintenanceBackupPending) || Boolean(currentRestorePending)}
                        title={maintenanceBusy === "backup-create" ? t.maintenanceBackupCreating : t.maintenanceBackupCreate}
                        aria-label={maintenanceBusy === "backup-create" ? t.maintenanceBackupCreating : t.maintenanceBackupCreate}
                        aria-busy={maintenanceBusy === "backup-create" ? "true" : undefined}
                      >
                        <img src="/assets/icons/ui/backup-create.svg" alt="" aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        className="button secondary small appIllustratedAction settingsMaintenanceActionIcon"
                        onClick={openMaintenanceBackupDetail}
                        disabled={Boolean(maintenanceBusy)}
                        title={t.maintenanceBackupOpenList}
                        aria-label={t.maintenanceBackupOpenList}
                      >
                        <img src="/assets/icons/ui/open.png" alt="" aria-hidden="true" />
                      </button>
                    </div>
                  </div>
                </section>
                </div>

                <section className="settingsMaintenanceSupport">
                  <div className="settingsMaintenanceSupportMain">
                    <div className="settingsMaintenanceSupportCopy">
                      <div className="settingsMaintenanceCardHeading settingsMaintenanceCardHeadingAligned">
                        <h3>{t.maintenanceSupportTitle}</h3>
                        <button type="button" className={`settingsMaintenancePill ${maintenanceWarnings.groups.actionable ? "is-warning" : "is-ok"}`} onClick={() => setMaintenanceDetail("warnings")} title={`${t.open}: ${t.maintenanceSupportTitle}`}>
                          {maintenanceWarnings.groups.actionable
                            ? `${t.maintenanceWarningActionable}: ${maintenanceWarnings.groups.actionable}`
                            : t.maintenanceSupportStatusOk}
                        </button>
                      </div>
                      <p>{t.maintenanceSupportText}</p>
                    </div>
                  </div>
                  <div className="settingsMaintenanceSupportActions">
                    <button
                      type="button"
                      className="button secondary small appIllustratedAction settingsMaintenanceActionIcon settingsMaintenanceSupportActionButton"
                      onClick={onOpenDiagnosticChoice}
                      disabled={Boolean(maintenanceBusy) || securityBusy}
                      title={t.maintenanceReportDownload}
                      aria-label={t.maintenanceReportDownload}
                    >
                      <img src="/assets/icons/ui/download-report.svg" alt="" aria-hidden="true" />
                    </button>
                  </div>
                </section>
                  </>
                ) : (
                  <section className="settingsMaintenanceBackupDetail">
                    <div className="settingsMaintenanceBackupDetailHeader">
                      <button
                        type="button"
                        className="settingsMaintenanceBackButton"
                        onClick={closeMaintenanceBackupDetail}
                        disabled={Boolean(maintenanceBusy) || Boolean(currentRestorePending)}
                      >
                        <span aria-hidden="true">←</span>
                        {t.maintenanceBackupBackToOverview}
                      </button>
                      <div>
                        <h3>{t.maintenanceBackupsTitle}</h3>
                        <p>{maintenanceBackupManager.statusText} · {t.maintenanceBackupTotalSize}: {maintenanceBackupManager.totalBytesText}</p>
                      </div>
                      <button
                        type="button"
                        className="button secondary small appIllustratedAction settingsMaintenanceActionIcon"
                        onClick={createMaintenanceBackup}
                        disabled={Boolean(maintenanceBusy) || Boolean(maintenanceBackupPending) || Boolean(currentRestorePending)}
                        title={maintenanceBusy === "backup-create" ? t.maintenanceBackupCreating : t.maintenanceBackupCreate}
                        aria-label={maintenanceBusy === "backup-create" ? t.maintenanceBackupCreating : t.maintenanceBackupCreate}
                        aria-busy={maintenanceBusy === "backup-create" ? "true" : undefined}
                      >
                        <img src="/assets/icons/ui/backup-create.svg" alt="" aria-hidden="true" />
                      </button>
                    </div>
                    <p className="settingsMaintenanceBackupScope">{t.maintenanceBackupScope}</p>
                    {maintenanceBusy === "backup-page" && !maintenanceBackupDetail ? (
                      <div className="settingsJournalEmpty">{t.checking}</div>
                    ) : maintenanceBackupManager.artifacts.length ? (
                      <div className="settingsMaintenanceBackupList">
                        <div className="settingsMaintenanceBackupListHead">
                          <span>{t.maintenanceBackupList}</span>
                          <span>
                            {t.maintenanceBackupPage
                              .replace("{start}", String(maintenanceBackupManager.pageStart))
                              .replace("{end}", String(maintenanceBackupManager.pageEnd))
                              .replace("{total}", String(maintenanceBackupManager.totalCount))}
                          </span>
                        </div>
                        {maintenanceBackupManager.artifacts.map((artifact) => (
                          <article className={`settingsMaintenanceBackupItem ${artifact.hasProblem ? "is-problem" : ""}`} key={artifact.id}>
                            <div className="settingsMaintenanceBackupItemBody">
                              <div className="settingsMaintenanceBackupItemHead">
                                <span className="settingsMaintenanceBackupCreatedAt">{artifact.createdAt}</span>
                                <span className="settingsMaintenanceBackupMeta">{t.maintenanceBackupSize}: {artifact.size} · {t.maintenanceBackupSchema}: {artifact.schema} · {artifact.backend}</span>
                              </div>
                              <div className="settingsMaintenanceBackupDetailRow">
                                <div className="settingsMaintenanceBackupStatusGrid">
                                  <div>
                                    <span>{t.maintenanceBackupAvailability}</span>
                                    <MaintenanceBackupDimensionStatus tone={artifact.availabilityTone} label={artifact.availabilityLabel} />
                                  </div>
                                  <div>
                                    <span>{t.maintenanceBackupIntegrity}</span>
                                    <MaintenanceBackupDimensionStatus tone={artifact.integrityTone} label={artifact.integrityLabel} />
                                  </div>
                                  <div>
                                    <span>{t.maintenanceBackupCompatibility}</span>
                                    <MaintenanceBackupDimensionStatus tone={artifact.compatibilityTone} label={artifact.compatibilityLabel} />
                                  </div>
                                  <div>
                                    <span>{t.maintenanceBackupValidation}</span>
                                    <MaintenanceBackupDimensionStatus tone={artifact.validationTone} label={artifact.validationLabel} />
                                  </div>
                                </div>
                                <div className="settingsMaintenanceBackupItemActions">
                                  <span
                                    className="settingsMaintenanceIconAction"
                                    title={artifact.canRestore
                                      ? t.maintenanceCurrentRestoreAction
                                      : currentRestoreReasonText(artifact.restoreIneligibleReason)}
                                  >
                                    <button
                                      type="button"
                                      className="settingsMaintenanceMiniButton"
                                      onClick={() => requestCurrentDatabaseRestore(artifact)}
                                      disabled={Boolean(maintenanceBusy) || Boolean(maintenanceBackupPending) || Boolean(currentRestorePending) || !artifact.canRestore}
                                      aria-label={artifact.canRestore
                                        ? t.maintenanceCurrentRestoreAction
                                        : `${t.maintenanceCurrentRestoreAction}: ${currentRestoreReasonText(artifact.restoreIneligibleReason)}`}
                                    >
                                      <MaintenanceRestoreIcon />
                                    </button>
                                  </span>
                                  <span className="settingsMaintenanceIconAction" title={maintenanceBusy === "backup-check" ? t.maintenanceBackupChecking : t.maintenanceBackupCheck}>
                                    <button
                                      type="button"
                                      className="settingsMaintenanceMiniButton"
                                      onClick={() => requestCheckMaintenanceBackup(artifact)}
                                      disabled={Boolean(maintenanceBusy) || Boolean(maintenanceBackupPending) || Boolean(currentRestorePending) || !artifact.canCheck}
                                      aria-label={maintenanceBusy === "backup-check" ? t.maintenanceBackupChecking : t.maintenanceBackupCheck}
                                      aria-busy={maintenanceBusy === "backup-check" ? "true" : undefined}
                                    >
                                      <MaintenanceCheckIcon />
                                    </button>
                                  </span>
                                  <span className="settingsMaintenanceIconAction" title={maintenanceBusy === "backup-delete" ? t.maintenanceBackupDeleting : t.maintenanceBackupDelete}>
                                    <button
                                      type="button"
                                      className="settingsMaintenanceMiniButton danger"
                                      onClick={() => requestDeleteMaintenanceBackup(artifact)}
                                      disabled={Boolean(maintenanceBusy) || Boolean(maintenanceBackupPending) || Boolean(currentRestorePending) || !artifact.deletable}
                                      aria-label={maintenanceBusy === "backup-delete" ? t.maintenanceBackupDeleting : t.maintenanceBackupDelete}
                                      aria-busy={maintenanceBusy === "backup-delete" ? "true" : undefined}
                                    >
                                      <MaintenanceTrashIcon />
                                    </button>
                                  </span>
                                </div>
                              </div>
                              {artifact.checkedAt || artifact.validatedAt ? (
                                <small className="settingsMaintenanceBackupEvidenceTime">
                                  {artifact.checkedAt ? t.maintenanceBackupCheckedAt.replace("{date}", artifact.checkedAt) : ""}
                                  {artifact.checkedAt && artifact.validatedAt ? " · " : ""}
                                  {artifact.validatedAt ? t.maintenanceBackupValidatedAt.replace("{date}", artifact.validatedAt) : ""}
                                </small>
                              ) : null}
                            </div>
                          </article>
                        ))}
                        <div className="settingsMaintenanceBackupPagination">
                          <span className="settingsMaintenanceIconAction" title={t.maintenanceBackupPrevious}>
                            <button
                              type="button"
                              className="settingsMaintenanceMiniButton"
                              onClick={() => loadMaintenanceBackupPage(Math.max(0, maintenanceBackupManager.offset - maintenanceBackupManager.limit))}
                              disabled={Boolean(maintenanceBusy) || !maintenanceBackupManager.hasPrevious}
                              aria-label={t.maintenanceBackupPrevious}
                            >
                              <span aria-hidden="true">←</span>
                            </button>
                          </span>
                          <span className="settingsMaintenanceIconAction" title={t.maintenanceBackupNext}>
                            <button
                              type="button"
                              className="settingsMaintenanceMiniButton"
                              onClick={() => loadMaintenanceBackupPage(maintenanceBackupManager.offset + maintenanceBackupManager.limit)}
                              disabled={Boolean(maintenanceBusy) || !maintenanceBackupManager.hasMore}
                              aria-label={t.maintenanceBackupNext}
                            >
                              <span aria-hidden="true">→</span>
                            </button>
                          </span>
                        </div>
                      </div>
                    ) : <div className="settingsJournalEmpty">{t.maintenanceBackupStatusEmpty}</div>}
                  </section>
                )}


              </div>
            ) : null}
          </div>
        </div>
      ) : null}
      <OperationDialog dialog={maintenanceModalOpen && maintenanceDetail ? {
        id: "maintenance-detail", className: "settingsMaintenanceDetailDialog", overlayClassName: "settingsMaintenanceDetailOverlay", tone: "neutral", closeLabel: t.close,
        title: maintenanceDetail === "update" ? t.updateApplyTitle : maintenanceDetail === "backup" ? t.maintenanceBackupsTitle : maintenanceDetail === "warnings" ? t.maintenanceSupportTitle : t.maintenanceDatabaseTitle,
        content: maintenanceDetail === "update" ? <>{updateDetails}{maintenanceActionResult?.flowKey === "update" ? actionDetails : null}</>
          : maintenanceDetail === "backup" ? <div role="status"><p>{maintenanceBackupPending || maintenanceBackupResult?.recovering ? maintenanceBackupProgressText : maintenanceBackupResultModel?.text || maintenanceBackupOverview.statusText}</p>{maintenanceBackupResultModel ? <strong>{maintenanceBackupResultModel.label}</strong> : null}</div>
          : maintenanceDetail === "warnings" ? <div>{maintenanceWarnings.items.filter((item) => item.classification === "actionable").map((item) => <article key={`${item.code}-${item.title}`}><strong>{item.title}</strong><p>{item.summary}</p><small>{item.action}</small></article>)}</div>
          : actionDetails,
      } : null} onClose={() => setMaintenanceDetail(null)} />
    </>
  );
}

export function SettingsUpdateApplyDialog({ controller, t }) {
  const {
    updateApplyDialog,
    confirmUpdateApply,
    closeUpdateApplyDialog,
  } = controller;

  return (
    <>
      <OperationDialog
        dialog={updateApplyDialog ? {
          id: "update-apply-confirm",
          presentation: "compact-confirmation",
          title: t.updateApplyModalTitle,
          message: t.updateApplyConfirm,
          overlayClassName: "settingsUpdateApplyDialogOverlay",
          tone: "warning",
          closeLabel: t.close,
          cancelLabel: t.cancel,
          confirmLabel: t.updateApplyModalConfirm,
          onConfirm: confirmUpdateApply,
        } : null}
        onClose={closeUpdateApplyDialog}
      />
    </>
  );
}
