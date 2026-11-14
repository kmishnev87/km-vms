package ru.kmvms.mobile.push

import org.json.JSONObject
import ru.kmvms.mobile.events.CameraEventKind
import java.time.Instant
import java.util.UUID

/** A push can display text, not carry a credential, media address or command. */
data class PushEnvelope(val eventId: String, val cameraId: Long?, val cameraName: String,
    val kind: CameraEventKind, val occurred: Instant, val test: Boolean = false, val objects: Set<CameraEventKind> = emptySet()) {
    companion object {
        fun parse(bytes: ByteArray): PushEnvelope {
            require(bytes.size in 1..3000)
            val value = JSONObject(bytes.toString(Charsets.UTF_8))
            require(value.getInt("v") == 1)
            val test = value.getString("type") == "delivery_test"
            require(test || value.getString("type") == "camera_event")
            val id = value.getString("event_id")
            require(id.length == 36 && UUID.fromString(id).toString() == id.lowercase())
            val cameraId = if (!value.has("camera_id") || value.isNull("camera_id")) null else value.getLong("camera_id").also { require(it > 0) }
            val name = value.optString("camera_name").take(160).filterNot(Char::isISOControl)
            val classes = value.optJSONArray("objects")
            val objects = if (classes == null) emptySet() else (0 until minOf(3, classes.length())).map {
                CameraEventKind.parse(classes.optString(it)) }.filter { it in setOf(CameraEventKind.PERSON, CameraEventKind.VEHICLE, CameraEventKind.ANIMAL) }.toSet()
            return PushEnvelope(id, cameraId, name, CameraEventKind.parse(value.optString("kind")),
                Instant.parse(value.getString("occurred_at")), test, objects)
        }
    }
}
