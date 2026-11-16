"use client";
import { useI18n } from "../lib/i18n";
import { CAMERA_EVENT_KINDS, minuteLabel, minuteValue } from "../lib/cameraEvents";
import { toggleEventPreference, changeCameraNotificationTime, notificationCameraGroups } from "../lib/cameraEventPreferences";

function TimeField({ value, disabled, label, onChange }) {
  return <input className="cameraEventsTimeInput" type="time" aria-label={label} value={minuteLabel(value)} disabled={disabled}
    onClick={event => { try { event.currentTarget.showPicker?.(); } catch {} }}
    onKeyDown={event => { if (event.key === "Escape") event.stopPropagation(); }}
    onChange={event => { if (event.target.value) onChange(minuteValue(event.target.value)); }}/>;
}

export default function CameraEventPreferences({ preference, cameras, busy, onChange, onSave }) {
  const { t } = useI18n();
  if (!preference) return <div className="cameraEventsEmpty">{t("cameraEvents.loading")}</div>;
  const individual = preference.schedule_mode === "per_camera";
  function patch(value) { onChange(previous => ({ ...previous, ...value })); }
  function toggle(key, item, all) {
    onChange(previous => toggleEventPreference(previous, key, item, all));
  }
  function changeCameraTime(camera, key, value) {
    onChange(previous => changeCameraNotificationTime(previous, camera.id, key, value));
  }
  return <div className="cameraEventsPreferences">
    <div className="cameraEventsPreferenceScroll">
      <div className="cameraEventsPreferenceIntro">
        <label className="cameraEventsCheck cameraEventsPreferenceMaster"><input type="checkbox" disabled={busy} checked={preference.notifications}
          onChange={event => patch({ notifications: event.target.checked })}/><span>{t("cameraEvents.notifyMe")}</span></label>
        <p>{t("cameraEvents.notificationHelp")}</p>
      </div>
      <div className="cameraEventsSchedule">
        <label className="cameraEventsCheck"><input type="checkbox" disabled={busy} checked={preference.hours_enabled}
          onChange={event => patch({ hours_enabled: event.target.checked })}/><span>{t("cameraEvents.schedule")}</span></label>
        <>
          <select className="cameraEventsScheduleMode" aria-label={t("cameraEvents.scheduleMode")} disabled={busy}
            value={preference.schedule_mode || "all"} onChange={event => patch({ schedule_mode: event.target.value })}>
            <option value="all">{t("cameraEvents.scheduleAll")}</option><option value="per_camera">{t("cameraEvents.schedulePerCamera")}</option>
          </select>
          <div className={`cameraEventsTimeRange${individual ? " isPlaceholder" : ""}`} aria-hidden={individual}>
            <label>{t("cameraEvents.from")}<TimeField value={preference.start_minute} disabled={busy} label={t("cameraEvents.from")}
              onChange={value => patch({ hours_enabled: true, start_minute: value })}/></label>
            <span className="cameraEventsTimeSeparator" aria-hidden="true">–</span>
            <label>{t("cameraEvents.to")}<TimeField value={preference.end_minute} disabled={busy} label={t("cameraEvents.to")}
              onChange={value => patch({ hours_enabled: true, end_minute: value })}/></label>
          </div>
        </>
      </div>
      <fieldset disabled={busy} aria-label={t("cameraEvents.notificationCameras")}>
        <div className={`cameraEventsCameraPreferences ${individual ? "hasSchedules" : ""}`}>
          {notificationCameraGroups(cameras).map((group, groupIndex) => <div className="cameraEventsCameraPreferenceGroup" key={group[0].id}>
            <div className="cameraEventsCameraPreferenceHead"><strong>{groupIndex === 0 ? t("cameraEvents.notificationCameras") : ""}</strong><span aria-hidden={!individual}>{t("cameraEvents.from")}</span><span aria-hidden="true"/><span aria-hidden={!individual}>{t("cameraEvents.to")}</span></div>
            {group.map(camera => {
            const selected = preference.camera_ids === null || Boolean(preference.camera_ids?.includes(camera.id));
            const schedule = preference.camera_schedules?.[camera.id] || preference;
            return <div className="cameraEventsCameraPreference" key={camera.id}>
              <label className="cameraEventsCheck"><input type="checkbox" checked={selected}
                onChange={() => toggle("camera_ids", camera.id, cameras.map(value => value.id))}/><span title={camera.name}>{camera.name}</span></label>
              {individual ? <>
                <TimeField value={schedule.start_minute} disabled={busy || !selected} label={`${camera.name}: ${t("cameraEvents.from")}`}
                  onChange={value => changeCameraTime(camera, "start_minute", value)}/>
                <span className="cameraEventsTimeSeparator" aria-hidden="true">–</span>
                <TimeField value={schedule.end_minute} disabled={busy || !selected} label={`${camera.name}: ${t("cameraEvents.to")}`}
                  onChange={value => changeCameraTime(camera, "end_minute", value)}/>
              </> : null}
            </div>;
            })}
          </div>)}
        </div>
      </fieldset>
      <small>{t("cameraEvents.scheduleHelp")}</small>
      <fieldset disabled={busy}><legend>{t("cameraEvents.notificationTypes")}</legend><div className="cameraEventsChoices">
        {CAMERA_EVENT_KINDS.map(kind => <label className="cameraEventsCheck" key={kind}>
          <input type="checkbox" checked={preference.kinds === null || Boolean(preference.kinds?.includes(kind))}
            onChange={() => toggle("kinds", kind, CAMERA_EVENT_KINDS)}/><span>{t(`cameraEvents.kinds.${kind}`)}</span>
        </label>)}
      </div></fieldset>
    </div>
    <div className="cameraEventsPreferenceFooter"><button type="button" className="button secondary" disabled={busy} onClick={onSave}>{t("cameraEvents.save")}</button></div>
  </div>;
}
