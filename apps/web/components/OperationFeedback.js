"use client";

import { useEffect, useRef } from "react";

let bodyScrollLockCount = 0;
let bodyOverflowBeforeLock = "";

export function useModalBodyScrollLock(active) {
  useEffect(() => {
    if (!active || typeof document === "undefined") return undefined;
    if (bodyScrollLockCount === 0) {
      bodyOverflowBeforeLock = document.body.style.overflow;
      document.body.style.overflow = "hidden";
    }
    bodyScrollLockCount += 1;
    return () => {
      bodyScrollLockCount = Math.max(0, bodyScrollLockCount - 1);
      if (bodyScrollLockCount === 0) {
        document.body.style.overflow = bodyOverflowBeforeLock;
        bodyOverflowBeforeLock = "";
      }
    };
  }, [active]);
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
  const titleId = `operation-dialog-title-${dialog.id || "current"}`;
  const descriptionId = `operation-dialog-description-${dialog.id || "current"}`;

  function requestClose() {
    if (canClose) onClose?.();
  }

  function handleKeyDown(event) {
    if (event.key === "Escape") {
      if (canClose) {
        event.preventDefault();
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
    <div className={`operationFeedbackOverlay ${dialog.overlayClassName || ""}`.trim()} role="presentation">
      <div
        ref={containerRef}
        className={`operationFeedbackDialog operationFeedbackDialog-${dialog.tone || "warning"} ${dialog.className || ""}`.trim()}
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
          {canClose ? (
            <button
              ref={closeRef}
              className="operationFeedbackClose"
              type="button"
              onClick={requestClose}
              aria-label={dialog.closeLabel || dialog.cancelLabel || "Close"}
            >
              ×
            </button>
          ) : null}
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
        {(hasConfirm || canClose || (dialog.actions || []).length) ? (
          <div className="operationFeedbackFooter">
            {(dialog.actions || []).map((action) => (
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
            {!hasConfirm && canClose ? (
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

export function OperationToast({ toast, onClose }) {
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    if (!toast) return undefined;
    const timeout = Number(toast.autoDismissMs ?? 2500);
    if (!Number.isFinite(timeout) || timeout <= 0) return undefined;
    const timer = window.setTimeout(() => onCloseRef.current?.(), timeout);
    return () => window.clearTimeout(timer);
  }, [toast?.id, toast?.autoDismissMs]);

  if (!toast) return null;
  return (
    <div className="operationFeedbackToastRegion" aria-live="polite" aria-atomic="true">
      <div className={`operationFeedbackToast operationFeedbackToast-${toast.tone || "success"}`} role="status">
        <div>
          <strong>{toast.title}</strong>
          {toast.message ? <span>{toast.message}</span> : null}
        </div>
        <button type="button" onClick={onClose} aria-label={toast.closeLabel || "Close"}>×</button>
      </div>
    </div>
  );
}
