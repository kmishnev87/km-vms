package ru.kmvms.mobile.media

import org.json.JSONObject
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull
import ru.kmvms.mobile.core.Stream
import ru.kmvms.mobile.core.OnlineMode
import ru.kmvms.mobile.data.NasApi

/** The same viewer/cookie protocol used by web; no global stream-stop operation. */
class LiveEndpoint(private val api: NasApi) {
    suspend fun open(cameraId: Long, stream: Stream): String {
        val value = api.json("api/live/viewers", "POST", request(cameraId, stream)) as JSONObject
        return value.getString("viewer_id").also { require(it.matches(Regex("[a-zA-Z0-9-]{1,128}"))) }
    }
    suspend fun touch(viewer: String): Boolean =
        (api.json("api/live/viewers/$viewer/touch", "POST", JSONObject()) as JSONObject).optBoolean("touched")
    suspend fun close(viewer: String) { api.json("api/live/viewers/$viewer", "DELETE") }
    suspend fun awaitReady(cameraId: Long, stream: Stream, viewer: String) {
        val ready = withTimeoutOrNull(60_000) {
            var polls = 0
            var available = false
            while (!available) {
                val status = api.json("api/live/status?camera_id=$cameraId&stream=${stream.apiKey}") as JSONObject
                val items = status.getJSONArray("items")
                available = (0 until items.length()).any { index -> items.getJSONObject(index).let {
                    it.optLong("camera_id") == cameraId && it.optString("stream") == stream.apiKey && it.optBoolean("ready") } }
                if (!available) {
                    // Preparation is not playback failure: retain the viewer while RTSP/HLS starts.
                    if (++polls % 10 == 0 && !touch(viewer)) throw java.io.IOException("Live viewer expired during startup")
                    delay(1500)
                }
            }
            true
        }
        if (ready != true) throw java.io.IOException("Live source startup timed out")
    }
    suspend fun authorize(cameraId: Long, stream: Stream): MediaLease {
        val value = api.json("api/live/media-token", "POST", request(cameraId, stream)) as JSONObject
        return MediaLease(value.getString("media_token"), value.getLong("expires_in"), OnlineMode.from(value.optString("online_mode")))
    }
    private fun request(id: Long, stream: Stream) = JSONObject().put("camera_id", id).put("stream", stream.apiKey)
}

/** Avoid generated toString containing the short-lived credential. */
class MediaLease(val token: String, val lifetimeSeconds: Long, val onlineMode: OnlineMode = OnlineMode.STABLE) {
    private val issuedNanos = System.nanoTime()
    init { require(lifetimeSeconds in 1..86400) }
    val renewAfterMillis: Long get() = ((lifetimeSeconds - 45).coerceAtLeast(1)) * 1000
    fun canRetryRenewal() = System.nanoTime() - issuedNanos < (lifetimeSeconds - 5).coerceAtLeast(0) * 1_000_000_000
    override fun toString() = "MediaLease([redacted])"
}
