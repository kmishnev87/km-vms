"use client";

import { recordingFormatCameraText, recordingFormatCopy, recordingFormatNotice } from "../lib/recordingFormatChange";
import { OperationDialog } from "./OperationFeedback";

export default function RecordingFormatChangeStatus({ status, lang, open, onClose }) {
  const copy = recordingFormatCopy(lang);
  const notice = recordingFormatNotice(status, lang);
  return (
    <OperationDialog
      dialog={open ? {
        id: "recording-format-details",
        tone: notice.state === "failed" ? "warning" : "info",
        title: copy.title,
        message: notice.message,
        items: (status?.items || []).map((item) => `${item.camera_name}: ${recordingFormatCameraText(item, lang)}`),
        closeLabel: copy.close,
      } : null}
      onClose={onClose}
    />
  );
}
