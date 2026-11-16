"use client";
import { useI18n } from "../lib/i18n";

export default function CameraEventReceiving({ cameras, busy, onChange, onRefresh }) {
  const { t } = useI18n();
  return <div className="cameraEventsReceiving">
    <div className="cameraEventsReceivingHead"><p>{t("cameraEvents.receivingHelp")}</p>
      <button className="cameraEventsIconButton" type="button" disabled={busy} onClick={onRefresh}
        title={t("cameraEvents.refresh")} aria-label={t("cameraEvents.refresh")}>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19.5 10A8 8 0 1 0 19 17M20 3v7h-7"/></svg>
      </button>
    </div>
    <div className="cameraEventsReceivingList">{cameras.map(camera => <div className="cameraEventsReceivingRow" key={camera.id}>
      <label className="cameraEventsCheck"><input type="checkbox" checked={camera.events_enabled} disabled={busy || camera.protocol !== "onvif"}
        aria-label={`${t("cameraEvents.receiveEvents")}: ${camera.name}`} onChange={event => onChange(camera, event.target.checked)}/>
        <span title={camera.name}>{camera.name}</span></label>
      <span>{t(camera.protocol !== "onvif" ? "cameraEvents.receivingUnsupported" : camera.events_enabled ? "cameraEvents.enabled" : "cameraEvents.disabled")}</span>
    </div>)}</div>
  </div>;
}
