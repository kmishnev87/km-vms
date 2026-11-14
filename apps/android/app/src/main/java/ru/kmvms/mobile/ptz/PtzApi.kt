package ru.kmvms.mobile.ptz

import org.json.JSONObject
import ru.kmvms.mobile.core.PtzCommand
import ru.kmvms.mobile.core.PtzResult
import ru.kmvms.mobile.data.NasApi

data class PtzCapabilities(val supported: Boolean?, val profile: String?, val panTilt: Boolean,
                           val zoom: Boolean, val presets: Boolean, val point: Boolean, val area: Boolean) {
    companion object { fun unknown() = PtzCapabilities(null, null, false, false, false, false, false) }
}
data class PtzPreset(val token: String, val name: String)

class PtzApi(private val api: NasApi) {
    suspend fun capabilities(id: Long): PtzCapabilities {
        val value = api.json("api/cameras/$id/onvif/ptz/capabilities") as JSONObject
        return PtzCapabilities(if (value.isNull("supported")) null else value.getBoolean("supported"),
            value.optString("profile_token").takeUnless { it.isBlank() || it == "null" },
            value.optBoolean("can_pan_tilt"), value.optBoolean("can_zoom"), value.optBoolean("can_presets"),
            value.optBoolean("can_point"), value.optBoolean("can_area_zoom"))
    }
    suspend fun presets(id: Long): List<PtzPreset> {
        val array = (api.json("api/cameras/$id/onvif/ptz/presets") as JSONObject).getJSONArray("presets")
        return (0 until array.length()).map { array.getJSONObject(it).let { value -> PtzPreset(value.getString("token"), value.optString("name")) } }
    }
    suspend fun send(id: Long, profile: String?, command: PtzCommand): PtzResult {
        val body = JSONObject().put("action", command.action).put("dry_run", false)
        profile?.let { body.put("profile_token", it) }
        command.controlId?.let { body.put("control_id", it).put("sequence", command.sequence) }
        if (command.action in setOf("move", "zoom")) body.put("direction", command.direction).put("speed", command.speed)
            .put("duration_seconds", 1.5).put("hold", true)
        command.preset?.let { body.put("preset_token", it) }
        if (command.action == "point") body.put("x", command.coordinates[0]).put("y", command.coordinates[1])
        if (command.action == "area_zoom") listOf("left", "top", "right", "bottom").forEachIndexed { index, key -> body.put(key, command.coordinates[index]) }
        val result = api.json("api/cameras/$id/onvif/ptz/command", "POST", body) as JSONObject
        return PtzResult(result.optBoolean("executed"), result.optBoolean("camera_stopped"), result.optBoolean("ignored_stale_control"),
            result.optString("movement_mode") == "continuous", result.optDouble("lease_seconds", 1.5))
    }
}
