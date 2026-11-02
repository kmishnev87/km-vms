"use client";

import { recordingFormatCameraText, recordingFormatCopy, recordingFormatNotice } from "../lib/recordingFormatChange";

export default function RecordingFormatChangeStatus({ status, lang }) {
  const notice = recordingFormatNotice(status, lang);
  return (
    <section className="settingsRecordingFormatStatus" data-state={notice.state} aria-label={recordingFormatCopy(lang).title} aria-live="polite">
      <strong>{recordingFormatCopy(lang).title}</strong>
      <p>{notice.message}</p>
      {status?.items?.length ? <ul>{status.items.map((item) => (
        <li key={item.camera_id}>
          <b>{item.camera_name}</b>: {recordingFormatCameraText(item, lang)}
        </li>
      ))}</ul> : null}
    </section>
  );
}
