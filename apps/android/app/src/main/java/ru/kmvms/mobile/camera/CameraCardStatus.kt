package ru.kmvms.mobile.camera

import org.json.JSONObject
import ru.kmvms.mobile.data.Camera

enum class CameraCardStatus { RECORDING, STOPPED, STARTING, UNKNOWN }

fun cameraCardStatus(camera: Camera, summary: JSONObject?): CameraCardStatus {
    if (!camera.enabled) return CameraCardStatus.STOPPED
    if (summary == null || summary.optJSONObject("heartbeat")?.optString("status") == "stale_or_unavailable") return CameraCardStatus.UNKNOWN
    val rows = summary.optJSONArray("camera_recording_states") ?: return CameraCardStatus.UNKNOWN
    val row = (0 until rows.length()).mapNotNull { rows.optJSONObject(it) }.find { it.optLong("camera_id") == camera.id }
        ?: return CameraCardStatus.UNKNOWN
    if (!row.optBoolean("heartbeat_current") || row.optBoolean("stale_current_segment") || row.optBoolean("current_failure")) return CameraCardStatus.STOPPED
    return when {
        row.optString("job_state") == "recording" && row.optBoolean("confirmed_recording") -> CameraCardStatus.RECORDING
        row.optString("job_state") in setOf("starting", "restarting") || row.optString("job_state") == "recording" -> CameraCardStatus.STARTING
        else -> CameraCardStatus.STOPPED
    }
}
