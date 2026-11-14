package ru.kmvms.mobile.events

import org.json.JSONArray
import org.json.JSONObject
import ru.kmvms.mobile.archive.encode
import ru.kmvms.mobile.archive.parseTime
import ru.kmvms.mobile.data.NasApi
import java.time.Instant
import java.time.ZoneId
import java.time.ZoneOffset
import java.util.UUID

enum class CameraEventKind(val code: String) {
    MOTION("motion"), PERSON("person"), VEHICLE("vehicle"), ANIMAL("animal"),
    INTRUSION("intrusion"), LINE_CROSSING("line_crossing"), LOITERING("loitering"),
    TAMPER("tamper"), AUDIO("audio"), WRONG_DIRECTION("wrong_direction"), FIRE("fire"), OTHER("other");
    companion object {
        fun parse(value: String) = entries.firstOrNull { it.code == value } ?: OTHER
        val useful: Set<CameraEventKind> get() = entries.filter { it != OTHER }.toSet()
    }
}

data class CameraEvent(
    val id: String, val cameraId: Long?, val camera: String, val kind: CameraEventKind,
    val objects: Set<CameraEventKind>, val occurred: Instant, val read: Boolean,
    val ambiguous: Boolean, val uncertainClock: Boolean,
) {
    companion object {
        fun from(value: JSONObject, zone: ZoneId): CameraEvent {
            val classes = value.optJSONArray("object_kinds")
            return CameraEvent(validEventId(value.getString("id")),
                if (value.has("camera_id") && !value.isNull("camera_id")) value.getLong("camera_id") else null,
                value.optString("camera_name"), CameraEventKind.parse(value.optString("kind")),
                (if (classes != null) (0 until classes.length()).map { CameraEventKind.parse(classes.optString(it)) }
                    else listOf(CameraEventKind.parse(value.optString("object_kind"))))
                    .filter { it in setOf(CameraEventKind.PERSON, CameraEventKind.VEHICLE, CameraEventKind.ANIMAL) }.toSet(),
                parseTime(value.getString("occurred_at"), zone) ?: throw IllegalArgumentException("event_time_invalid"),
                value.optBoolean("read"), value.optBoolean("source_ambiguous"), value.optBoolean("clock_uncertain"))
        }
    }
}

data class EventFilter(val cameraId: Long? = null, val kind: CameraEventKind? = null, val days: Int = 7, val unread: Boolean = false)
data class EventPage(val items: List<CameraEvent>, val total: Int, val offset: Int, val more: Boolean,
                     val unread: Int, val cursor: String?, val historyCount: Int, val zone: ZoneId,
                     val since: String = Instant.EPOCH.toString(), val selectionId: String? = null, val visibleIds: Set<String> = emptySet())
data class EventSelection(val id: String, val count: Int, val since: String,
                          val visibleIds: Set<String>, val excluded: Set<String> = emptySet()) {
    val selectedCount get() = (count-excluded.size).coerceAtLeast(0)
    fun contains(id: String) = id in visibleIds && id !in excluded
}
data class EventCamera(val id: Long, val name: String, val receiving: Boolean)
data class EventPreferences(val notifications: Boolean = true, val cameraIds: Set<Long>? = null,
    val kinds: Set<CameraEventKind>? = null, val hoursEnabled: Boolean = false, val startMinute: Int = 0, val endMinute: Int = 0) {
    fun json() = JSONObject().put("notifications", notifications)
        .put("camera_ids", cameraIds?.let { JSONArray(it.toList()) } ?: JSONObject.NULL)
        .put("kinds", kinds?.let { JSONArray(it.map(CameraEventKind::code)) } ?: JSONObject.NULL)
        .put("hours_enabled", hoursEnabled).put("start_minute", startMinute).put("end_minute", endMinute)
    companion object {
        fun from(value: JSONObject): EventPreferences {
            val ids = value.optJSONArray("camera_ids")
            val kinds = value.optJSONArray("kinds")
            return EventPreferences(value.optBoolean("notifications", true),
                ids?.let { (0 until it.length()).map(it::getLong).toSet() },
                kinds?.let { (0 until it.length()).map { index -> CameraEventKind.parse(it.getString(index)) }.toSet() },
                value.optBoolean("hours_enabled"), value.optInt("start_minute"), value.optInt("end_minute"))
        }
    }
}

data class EventDeletion(val ids: Set<String>? = null, val through: String? = null, val count: Int,
                         val snapshot: EventSelection? = null) {
    init { require(listOf(ids,through,snapshot).count { it != null } == 1) }
}

class EventsApi(val api: NasApi) {
    suspend fun single(id: String): Pair<CameraEvent, ZoneId> {
        val body = api.json("api/mobile/push/events/${validEventId(id)}") as JSONObject
        val zone = ZoneId.of(body.getString("timezone"))
        return CameraEvent.from(body.getJSONObject("event"), zone) to zone
    }
    suspend fun page(filter: EventFilter, offset: Int = 0, selection: EventSelection? = null): EventPage {
        require(filter.days in setOf(1, 7, 30))
        val since = selection?.since ?: Instant.now().minusSeconds(filter.days * 86400L).toString()
        val path = "api/camera-events?limit=30&offset=${offset.coerceAtLeast(0)}" +
            "&since=${encode(since)}" +
            (filter.cameraId?.let { "&camera_id=$it" } ?: "") +
            (filter.kind?.let { "&kind=${it.code}" } ?: "") + (if (filter.unread) "&unread=true" else "") +
            (selection?.let { "&selection_id=${validEventId(it.id)}" } ?: "")
        val body = api.json(path) as JSONObject
        val zone = runCatching { ZoneId.of(body.getString("timezone")) }.getOrDefault(ZoneOffset.UTC)
        val rows = body.getJSONArray("items")
        return EventPage((0 until rows.length()).map { CameraEvent.from(rows.getJSONObject(it), zone) },
            body.getInt("total"), body.optInt("offset"), body.optInt("offset") + rows.length() < body.getInt("total"),
            body.optInt("unread_count"), body.optString("read_cursor").takeUnless { it.isBlank() || it == "null" },
            body.optInt("history_count"), zone, since,
            body.optJSONObject("selection")?.optString("id"),
            body.optJSONObject("selection")?.optJSONArray("visible_ids")?.strings().orEmpty())
    }
    suspend fun selectFiltered(filter: EventFilter, page: EventPage): EventSelection {
        val request = JSONObject().put("through",requireNotNull(page.cursor)).put("since",page.since)
            .put("unread",filter.unread).put("visible_ids",JSONArray(page.items.map { it.id }))
        filter.cameraId?.let { request.put("camera_id",it) }
        filter.kind?.let { request.put("kind",it.code) }
        val result = api.json("api/camera-events/selections","POST",request) as JSONObject
        return EventSelection(validEventId(result.getString("id")),result.getInt("count").also{require(it in 1..100_000)},
            page.since,result.optJSONArray("visible_ids")?.strings().orEmpty())
    }
    suspend fun readSelection(selection: EventSelection) {
        api.json("api/camera-events/selections/${validEventId(selection.id)}/read","POST",selection.action())
    }
    suspend fun cameras(): List<EventCamera> {
        val rows = (api.json("api/camera-events/cameras") as JSONObject).getJSONArray("items")
        return (0 until rows.length()).map { index -> rows.getJSONObject(index).let {
            EventCamera(it.getLong("id"), it.getString("name"), it.optBoolean("events_enabled"))
        } }
    }
    suspend fun preferences() = EventPreferences.from(api.json("api/camera-events/preferences") as JSONObject)
    suspend fun save(value: EventPreferences): EventPreferences {
        require(value.startMinute in 0..1439 && value.endMinute in 0..1439)
        return EventPreferences.from(api.json("api/camera-events/preferences", "PUT", value.json()) as JSONObject)
    }
    suspend fun read(ids: Set<String>) {
        require(ids.size in 1..200)
        api.json("api/camera-events/read", "POST", JSONObject().put("ids", JSONArray(ids.map(::validEventId))))
    }
    suspend fun readAll(through: String) {
        require(through.length in 39..100)
        api.json("api/camera-events/read-all", "POST", JSONObject().put("through", through))
    }
    suspend fun delete(target: EventDeletion) {
        target.snapshot?.let {
            api.json("api/camera-events/selections/${validEventId(it.id)}/delete","POST",it.action())
            return
        }
        val body = JSONObject()
        if (target.ids != null) {
            require(target.ids.size in 1..200)
            body.put("ids", JSONArray(target.ids.map(::validEventId)))
        } else { require(target.through!!.length in 39..100); body.put("through", target.through) }
        api.json(if (target.ids != null) "api/camera-events/delete" else "api/camera-events/delete-all", "POST", body)
    }
    suspend fun updates(cursor: String?): JSONObject = api.longPoll("api/camera-events/updates?wait=${if (cursor == null) 0 else 25}" +
        (cursor?.let { "&after=${encode(it)}" } ?: "")) as JSONObject
}

private fun JSONArray.strings() = (0 until length()).map { validEventId(getString(it)) }.toSet()
private fun EventSelection.action() = JSONObject().put("excluded_ids",JSONArray(excluded.map(::validEventId)))

private fun validEventId(value: String) = value.also {
    require(it.length == 36 && UUID.fromString(it).toString() == it.lowercase())
}
