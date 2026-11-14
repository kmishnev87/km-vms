package ru.kmvms.mobile.data

import org.json.JSONObject
import ru.kmvms.mobile.core.Stream
import java.util.UUID

data class ServerProfile(val id: String = UUID.randomUUID().toString(), val name: String,
                         val address: String, val username: String)
data class NasUser(val id: Long, val name: String, val role: String, val permissions: Set<String>) {
    fun can(permission: String) = permission in permissions
}
data class Camera(val id: Long, val name: String, val enabled: Boolean, val protocol: String,
                  val host: String, val status: String, val hasSub: Boolean,
                  val segmentMinutes: Int?, val retentionDays: Int?, val quotaGb: Int?, val mode: String?,
                  val defaultStream: Stream = Stream.SUB, val mainAspect: Float? = null, val subAspect: Float? = null,
                  val deleted: Boolean = false) {
    companion object {
        fun fromJson(item: JSONObject) = Camera(
            item.getLong("id"), item.getString("name"), item.optBoolean("enabled"), item.optString("protocol"),
            item.optString("host"), item.optString("status"),
            when (val sub = item.opt("rtsp_sub_url")) { is Boolean -> sub; is String -> sub.isNotBlank(); else -> false },
            if (item.has("segment_minutes")) item.optInt("segment_minutes") else null,
            if (item.has("retention_days")) item.optInt("retention_days") else null,
            if (item.has("storage_quota_gb")) item.optInt("storage_quota_gb") else null,
            item.optString("recording_mode").takeIf { it.isNotBlank() },
            if (item.optString("default_live_stream") == "main") Stream.MAIN else Stream.SUB,
            aspect(item.optJSONObject("stream_dimensions")?.optJSONObject("main")),
            aspect(item.optJSONObject("stream_dimensions")?.optJSONObject("sub")),
            item.optBoolean("deleted"),
        )
        private fun aspect(value: JSONObject?): Float? {
            val width = value?.optInt("width") ?: 0
            val height = value?.optInt("height") ?: 0
            return if (width > 0 && height > 0) width.toFloat() / height else null
        }
    }
}

/** Intentionally not a data class: generated toString must not expose a session. */
class SavedSignIn(val password: String, val access: String) {
    override fun toString() = "SavedSignIn([redacted])"
}
enum class ApiProblem { ADDRESS, AUTHENTICATION, FORBIDDEN, RATE_LIMIT, CONNECTION, SERVER, SAVED_SIGN_IN }
class NasException(val problem: ApiProblem, val httpStatus: Int? = null) : Exception(problem.name)
