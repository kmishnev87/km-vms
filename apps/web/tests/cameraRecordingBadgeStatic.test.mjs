import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const root = process.cwd().endsWith("apps/web") ? process.cwd() : join(process.cwd(), "apps/web");
const cameraPage = readFileSync(join(root, "app/cameras/page.js"), "utf8");
const i18n = readFileSync(join(root, "lib/i18n.js"), "utf8");

const staleCondition = 'if (jobState === "recording" && staleCurrentSegment)';
const healthyCondition = 'if (jobState === "recording" && confirmedRecording && !currentFailure)';

assert(cameraPage.includes("runtime?.stale_current_segment === true"));
assert(cameraPage.includes('String(runtime?.recording_health || "").toLowerCase() === "degraded"'));
assert(cameraPage.includes(staleCondition));
assert(cameraPage.includes("copy.recordingStale"));
assert(cameraPage.indexOf(staleCondition) < cameraPage.indexOf(healthyCondition));
assert(cameraPage.includes('if (jobState === "recording" && !confirmedRecording && !currentFailure) return { text: copy.starting'));

assert(i18n.includes('recordingStale: "Запись зависла"'));
assert(i18n.includes('recordingStale: "Recording stalled"'));
assert(i18n.includes('recordingStale: "录像停滞"'));
