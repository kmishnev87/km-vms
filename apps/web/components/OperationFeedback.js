"use client";

import { useEffect, useRef } from "react";
import { acquireModalScrollLock } from "../lib/modalScrollLock";

export function useModalBodyScrollLock(active, { freezePosition = false } = {}) {
  useEffect(() => {
    return active ? acquireModalScrollLock({ freezePosition }) : undefined;
  }, [active, freezePosition]);
}

function focusableElements(container) {
  if (!container) return [];
  return Array.from(
    container.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )
  ).filter((element) => !element.hasAttribute("hidden") && element.getAttribute("aria-hidden") !== "true");
}

export function OperationDialog({ dialog, onClose }) {
  const containerRef = useRef(null);
  const cancelRef = useRef(null);
  const closeRef = useRef(null);
  const returnFocusRef = useRef(null);
  const dialogOpen = Boolean(dialog);

  useModalBodyScrollLock(dialogOpen);

  useEffect(() => {
    if (!dialogOpen) return undefined;
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    return () => {
      if (returnFocusRef.current?.isConnected) returnFocusRef.current.focus({ preventScroll: true });
      returnFocusRef.current = null;
    };
  }, [dialogOpen]);

  useEffect(() => {
    if (!dialogOpen) return undefined;
    const timer = window.setTimeout(() => {
      const activeElement = document.activeElement;
      const activeInsideDialog = activeElement instanceof HTMLElement && containerRef.current?.contains(activeElement);
      if (!dialog?.busy && activeInsideDialog && activeElement !== containerRef.current) return;
      const preferred = dialog?.busy
        ? containerRef.current
        : cancelRef.current || closeRef.current || focusableElements(containerRef.current)[0] || containerRef.current;
      preferred?.focus({ preventScroll: true });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [dialog?.id, dialog?.busy, dialogOpen]);

  if (!dialog) return null;
  const hasConfirm = typeof dialog.onConfirm === "function";
  const canClose = !dialog.busy && dialog.dismissible !== false;
  const dialogActions = Array.isArray(dialog.actions) ? dialog.actions : [];
  const showStandaloneClose = !hasConfirm && canClose && dialog.showFooterClose !== false;
  const titleId = `operation-dialog-title-${dialog.id || "current"}`;
  const descriptionId = `operation-dialog-description-${dialog.id || "current"}`;
  const compactConfirmation = dialog.presentation === "compact-confirmation";
  const neutralChoice = dialog.presentation === "neutral-choice";
  const presentationClass = compactConfirmation
    ? "operationFeedbackDialog-compactConfirmation"
    : neutralChoice
      ? "operationFeedbackDialog-neutralChoice"
      : "";

  function requestClose() {
    if (canClose) onClose?.();
  }

  function handleKeyDown(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (canClose) {
        requestClose();
      }
      return;
    }
    if (event.key !== "Tab") return;
    const elements = focusableElements(containerRef.current);
    if (!elements.length) {
      event.preventDefault();
      containerRef.current?.focus({ preventScroll: true });
      return;
    }
    const first = elements[0];
    const last = elements[elements.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div
      className={`operationFeedbackOverlay ${compactConfirmation ? "operationFeedbackOverlay-compactConfirmation" : ""} ${dialog.overlayClassName || ""}`.trim()}
      role="presentation"
    >
      <div
        ref={containerRef}
        className={`operationFeedbackDialog operationFeedbackDialog-${dialog.tone || "warning"} ${presentationClass} ${dialog.className || ""}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        aria-busy={dialog.busy ? "true" : "false"}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <div className="operationFeedbackHead">
          <h2 id={titleId}>{dialog.title}</h2>
          {(
            <button
              ref={closeRef}
              className="operationFeedbackClose"
              type="button"
              onClick={requestClose}
              disabled={!canClose}
              style={canClose ? undefined : { visibility: "hidden" }}
              aria-hidden={canClose ? undefined : true}
              aria-label={dialog.closeLabel || dialog.cancelLabel || "Close"}
            >
              ×
            </button>
          )}
        </div>
        <div id={descriptionId} className="operationFeedbackBody" aria-live={dialog.busy ? "polite" : undefined}>
          {dialog.message ? <p>{dialog.message}</p> : null}
          {dialog.busy ? <div className="operationFeedbackBusy" aria-hidden="true"><span /></div> : null}
          {dialog.content || null}
          {Array.isArray(dialog.summary) && dialog.summary.length ? (
            <dl className="operationFeedbackSummary">
              {dialog.summary.map((item) => (
                <div key={`${item.label}-${item.value}`}>
                  <dt>{item.label}</dt>
                  <dd>{item.value}</dd>
                </div>
              ))}
            </dl>
          ) : null}
          {Array.isArray(dialog.descriptions) && dialog.descriptions.length ? (
            <div className={`operationFeedbackDescriptions ${neutralChoice ? "operationFeedbackDescriptions-neutralChoice" : ""}`.trim()}>
              {dialog.descriptions.map((item) => (
                <p key={`${item.label}-${item.value}`}>
                  {neutralChoice && item.label ? (
                    <>
                      <strong>{item.label}</strong>
                      <span>{item.value}</span>
                    </>
                  ) : item.label ? `${item.label} — ${item.value}` : item.value}
                </p>
              ))}
            </div>
          ) : null}
          {Array.isArray(dialog.items) && dialog.items.length ? (
            <ul className="operationFeedbackList">
              {dialog.items.map((item, index) => <li key={`${typeof item === "string" ? item : item?.label}-${index}`}>{typeof item === "string" ? item : item?.label}</li>)}
            </ul>
          ) : null}
          {Array.isArray(dialog.reasons) && dialog.reasons.length ? (
            <div className="operationFeedbackReasons">
              {dialog.reasons.map((reason) => (
                <div key={reason.code || reason.label}>
                  <strong>{reason.label}</strong>
                  {reason.count != null ? <span>{reason.count}</span> : null}
                  {reason.detail ? <p>{reason.detail}</p> : null}
                </div>
              ))}
            </div>
          ) : null}
          {(dialog.detail || dialog.action) ? <div className="operationFeedbackDetail">{dialog.detail || dialog.action}</div> : null}
        </div>
        {(hasConfirm || dialogActions.length || showStandaloneClose) ? (
          <div className={`operationFeedbackFooter ${neutralChoice ? "operationFeedbackFooter-neutralChoice" : ""}`.trim()}>
            {dialogActions.map((action) => (
              <button
                className="button secondary small"
                type="button"
                key={action.id || action.label}
                onClick={action.onClick}
                disabled={dialog.busy || action.disabled}
              >
                {action.label}
              </button>
            ))}
            {hasConfirm ? (
              <button
                ref={cancelRef}
                className="button secondary small"
                type="button"
                onClick={requestClose}
                disabled={!canClose}
              >
                {dialog.cancelLabel}
              </button>
            ) : null}
            {hasConfirm ? (
              <button
                className={`button small ${dialog.confirmTone === "danger" ? "dangerButton" : ""}`}
                type="button"
                onClick={dialog.onConfirm}
                disabled={dialog.busy || dialog.confirmDisabled}
              >
                {dialog.confirmLabel}
              </button>
            ) : null}
            {showStandaloneClose ? (
              <button ref={cancelRef} className="button secondary small" type="button" onClick={requestClose}>
                {dialog.closeLabel || dialog.cancelLabel}
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function OperationRefreshButton({ busy = false, error = "", label, onClick }) {
  return <button type="button" className={`operationRefreshButton${error ? " hasError" : ""}`} onClick={onClick}
    title={error || label} aria-label={error ? `${label}: ${error}` : label} aria-busy={busy || undefined}
    disabled={busy}>{error ? "!" : "↻"}</button>;
}

export function OperationToast({ toast, onClose }) {
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    if (!toast) return undefined;
    const timer = window.setTimeout(() => onCloseRef.current?.(), toast.action ? 8000 : 2500);
    return () => window.clearTimeout(timer);
  }, [toast]);

  if (!toast) return null;
  const tone = ["success", "error", "warning", "info"].includes(toast.tone) ? toast.tone : "info";
  const role = tone === "error" ? "alert" : "status";
  return (
    <div className="operationFeedbackToastRegion" aria-live="polite" aria-atomic="true">
      <div className={`operationFeedbackToast operationFeedbackToast-${tone}`} role={role}>
        <div>
          <strong>{toast.title}</strong>
          {toast.message ? <span>{toast.message}</span> : null}
          {toast.action ? (
            <button
              className="button secondary small operationFeedbackToastAction"
              type="button"
              onClick={() => { onCloseRef.current?.(); toast.action.onClick(); }}
            >
              {toast.action.label}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
