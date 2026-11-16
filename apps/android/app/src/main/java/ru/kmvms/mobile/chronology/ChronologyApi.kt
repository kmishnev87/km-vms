package ru.kmvms.mobile.chronology

import org.json.JSONObject
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import ru.kmvms.mobile.archive.encode
import ru.kmvms.mobile.archive.parseTime
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.data.Camera
import org.json.JSONArray
import ru.kmvms.mobile.media.MediaLease
import java.time.Instant
import java.time.ZoneId
import java.time.ZoneOffset
import kotlin.math.roundToLong

data class TimelineRange(val start: Long, val end: Long) {
    fun contains(position: Long) = position >= start && position < end
}

data class TimelineRanges(val zone: ZoneId, val cameras: Map<Long, List<TimelineRange>>)

data class TimelineSource(
    val cameraId: Long,
    val segmentId: Long,
    val root: String,
    val reference: String?,
    val start: Long,
    val end: Long,
    val mime: String?,
) {
    val key: String get() = "$cameraId:$root:$segmentId"
    fun covers(position: Long) = position >= start && position < end
    fun query() = reference?.let { "playback_ref=${encode(it)}" }
        ?: "segment_id=$segmentId&archive_root_id=${encode(root)}"
}

sealed interface TimelineLookup {
    data class Video(val source: TimelineSource) : TimelineLookup
    data object Empty : TimelineLookup
    data object Unavailable : TimelineLookup
}

class ChronologyApi(val api: NasApi, initialZone: ZoneId? = null) {
    suspend fun cameras(): List<Camera> {
        val rows = api.json("api/chronology/cameras") as JSONArray
        return (0 until rows.length()).map { Camera.fromJson(rows.getJSONObject(it)) }
    }
    @Volatile private var serverZone: ZoneId? = initialZone
    private val zoneLock = Mutex()
    private suspend fun timezone(): ZoneId = serverZone ?: zoneLock.withLock {
        serverZone ?: ZoneId.of((api.json("api/system/status") as JSONObject).getString("timezone"))
            .also { serverZone = it }
    }
    // Resolve the installation timezone before the FIRST local-naive request.
    // Learning it from ranges afterwards can leave a cached wrong-hour source.
    private suspend fun timestamp(instant: Instant) = instant.atZone(timezone()).toLocalDateTime().toString()
    suspend fun ranges(cameraIds: List<Long>, from: Instant, to: Instant): TimelineRanges {
        require(cameraIds.isNotEmpty() && cameraIds.all { it > 0 } && to > from)
        val body = api.json("api/chronology/ranges?camera_ids=${cameraIds.distinct().joinToString(",")}" +
            "&from=${encode(timestamp(from))}&to=${encode(timestamp(to))}") as JSONObject
        val zone = runCatching { ZoneId.of(body.getJSONObject("timezone").getString("id")) }.getOrDefault(ZoneOffset.UTC)
        serverZone = zone
        val items = body.getJSONObject("items")
        return TimelineRanges(zone, cameraIds.distinct().associateWith { id ->
            val ranges = items.optJSONObject(id.toString())?.optJSONArray("ranges")
            if (ranges == null) emptyList() else (0 until ranges.length()).mapNotNull { index ->
                val range = ranges.getJSONObject(index)
                val start = parseTime(range.optString("start_system").ifBlank { range.optString("start") }, zone)?.toEpochMilli()
                val end = parseTime(range.optString("end_system").ifBlank { range.optString("end") }, zone)?.toEpochMilli()
                if (start != null && end != null && end > start) TimelineRange(start, end) else null
            }.sortedBy { it.start }
        })
    }

    suspend fun resolve(cameraId: Long, position: Long): TimelineLookup {
        require(cameraId > 0)
        // Match the existing browser contract: a NAS-local timestamp allows the
        // server to read both current UTC storage and older local-naive archives.
        // An explicit offset intentionally opts out of that legacy read path.
        val body = api.json("api/chronology/playback?camera_id=$cameraId&ts=${encode(timestamp(Instant.ofEpochMilli(position)))}") as JSONObject
        if (!body.optBoolean("has_video")) {
            return if (body.optString("availability_status", "no_recording") == "no_recording") TimelineLookup.Empty
                else TimelineLookup.Unavailable
        }
        require(body.getLong("camera_id") == cameraId)
        val root = body.getString("archive_root_id").also { require(it.isNotBlank()) }
        val duration = body.getDouble("duration_sec")
        val offset = body.getDouble("offset_sec")
        require(duration.isFinite() && duration > 0 && offset.isFinite() && offset >= 0 && offset < duration)
        // The NAS supports legacy local-naive archive timestamps. Its offset is
        // already resolved against the requested instant; never assume file_start
        // without a timezone is UTC and shift an old archive by several hours.
        val start = position - (offset * 1000).roundToLong()
        return TimelineLookup.Video(TimelineSource(cameraId, body.getLong("segment_id"), root,
            body.optString("playback_ref").takeUnless { it.isBlank() || it == "null" },
            start, start + (duration * 1000).roundToLong(),
            body.optString("mime_type").takeUnless { it.isBlank() || it == "null" }))
    }

    suspend fun authorize(source: TimelineSource): MediaLease {
        val body = api.json("api/chronology/media-token?${source.query()}", "POST", JSONObject()) as JSONObject
        return MediaLease(body.getString("media_token"), body.getLong("expires_in"))
    }
}
