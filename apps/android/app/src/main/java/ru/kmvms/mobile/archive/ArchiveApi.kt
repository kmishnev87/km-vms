package ru.kmvms.mobile.archive

import org.json.JSONObject
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.media.MediaLease
import java.net.URLEncoder
import java.time.*

private fun JSONObject.optionalText(key: String) = optString(key).takeUnless { it.isBlank() || it == "null" }
data class ArchiveCamera(val id: Long, val name: String, val deleted: Boolean, val value: String = "camera:$id", val filterById: Boolean = false)
data class Recording(val id: Long, val root: String?, val reference: String?, val cameraId: Long,
                     val camera: String, val filename: String, val displayTime: String,
                     val startedSystem: String?, val endedSystem: String?, val mime: String?,
                     val size: String, val canPlay: Boolean, val canDownload: Boolean = canPlay) {
    val identityKey: String get() = reference ?: "${root.orEmpty()}:$id"
    fun identity() = JSONObject().apply {
        if (reference != null) put("recording_ref", reference)
        else { put("segment_id", id); root?.let { put("archive_root_id", it) } }
    }
    fun query() = if (reference != null) "recording_ref=" + encode(reference)
        else "segment_id=$id" + (root?.let { "&archive_root_id=" + encode(it) } ?: "")
    fun instant(zone: ZoneId): Instant? = startedSystem?.let { parseTime(it, zone) }
    companion object {
        fun fromJson(value: JSONObject) = Recording(value.getLong("segment_id"), value.optionalText("archive_root_id"),
            value.optionalText("recording_ref"), value.getLong("camera_id"), value.getString("camera"),
            value.getString("filename"), value.optString("created_at"), value.optionalText("started_at_system"),
            value.optionalText("ended_at_system"), value.optionalText("mime_type"), value.optString("size_human"),
            value.optBoolean("playback_available"), value.optBoolean("download_available"))
    }
}
data class RecordingPage(val items: List<Recording>, val total: Int, val more: Boolean, val zone: ZoneId)
data class ArchiveOptions(val names: List<String>, val exportable: List<ArchiveCamera>, val cameras: List<ArchiveCamera>)
data class Clip(val id: String, val title: String, val camera: String, val status: String,
                val progress: Int, val filename: String?, val size: Long, val canDownload: Boolean,
                val start: String?, val end: String?, val gaps: Int = 0, val errorCode: String? = null,
                val durationSeconds: Double? = null) {
    companion object {
        fun fromJson(value: JSONObject) = Clip(value.getString("id"), value.optionalText("title") ?: value.optionalText("filename").orEmpty(),
            value.optJSONObject("camera")?.optString("label").orEmpty(), value.getString("status"), value.optInt("progress_percent"),
            value.optionalText("filename"), value.optLong("output_size_bytes"), value.optBoolean("download_ready"),
            value.optionalText("start_ts"), value.optionalText("end_ts"), value.optJSONArray("gap_warnings")?.length() ?: 0,
            value.optionalText("error_code")?.takeIf { it == "source_gap_detected" },
            value.optDouble(if (value.has("output_duration_seconds")) "output_duration_seconds" else "duration_seconds")
                .takeIf { it.isFinite() && it > 0 })
    }
}
class ArchiveApi(val api: NasApi) {
    suspend fun options(): ArchiveOptions {
        val value = api.json("api/recordings/cameras") as JSONObject
        val names = value.getJSONArray("items"); val exportable = value.getJSONArray("export_items")
        val legacyNames = (0 until names.length()).map { names.getString(it) }
        val exportItems = (0 until exportable.length()).map {
            exportable.getJSONObject(it).let { item -> ArchiveCamera(item.getString("id").toLong(), item.getString("name"), item.optBoolean("deleted")) }
        }
        val browser = value.optJSONArray("camera_items")
        val cameras = if (browser != null) (0 until browser.length()).map { index ->
            val item = browser.getJSONObject(index)
            ArchiveCamera(item.getString("id").toLong(), item.getString("name"), item.optBoolean("deleted"), item.getString("value"), true)
        } else legacyNames.map { name ->
            val old = exportItems.find { it.name == name }
            ArchiveCamera(old?.id ?: 0, name, old?.deleted == true, name)
        }
        return ArchiveOptions(legacyNames, exportItems, cameras.sortedBy { it.deleted })
    }
    suspend fun list(camera: String? = null, date: LocalDate? = null, offset: Int = 0, cameraId: Long? = null): RecordingPage {
        require(cameraId == null || cameraId > 0)
        val path = "api/recordings?limit=30&offset=$offset" + (cameraId?.let { "&camera_id=$it" } ?: camera?.let { "&camera=" + encode(it) } ?: "") +
            (date?.let { "&date=$it" } ?: "")
        val value = api.json(path) as JSONObject
        val items = value.getJSONArray("items")
        val zone = try { ZoneId.of(value.getJSONObject("timezone").getString("id")) } catch (_: Exception) { ZoneOffset.UTC }
        return RecordingPage((0 until items.length()).map { Recording.fromJson(items.getJSONObject(it)) }, value.getInt("total"),
            value.getJSONObject("pagination").optBoolean("has_more"), zone)
    }
    suspend fun authorize(recording: Recording): MediaLease = authorizeRecording(recording, "stream")

    suspend fun authorizeDownload(recording: Recording): MediaLease = authorizeRecording(recording, "download")

    private suspend fun authorizeRecording(recording: Recording, action: String): MediaLease {
        val value = api.json("api/recordings/media-token", "POST", recording.identity().put("action", action)) as JSONObject
        return MediaLease(value.getString("media_token"), value.getLong("expires_in"))
    }
    suspend fun deleteRecording(recording: Recording, operationId: String) {
        require(java.util.UUID.fromString(operationId).toString() == operationId)
        val result = api.json("api/recordings?${recording.query()}&operation_id=$operationId", "DELETE") as JSONObject
        check(result.optBoolean("ok"))
    }
    suspend fun clips(offset: Int = 0): Pair<List<Clip>, Boolean> {
        val value = api.json("api/archive/exports?limit=30&offset=$offset") as JSONObject
        val items = value.getJSONArray("items")
        return (0 until items.length()).map { Clip.fromJson(items.getJSONObject(it)) }
            .filter { it.status in setOf("done", "queued", "running", "failed") } to value.optBoolean("has_more")
    }
    suspend fun createClip(cameraId: Long, title: String, start: OffsetDateTime, end: OffsetDateTime): Clip {
        val body = JSONObject().put("camera_id", cameraId).put("title", title).put("start_ts", start.toString()).put("end_ts", end.toString())
        return Clip.fromJson(api.json("api/archive/exports", "POST", body) as JSONObject)
    }
    suspend fun generate(id: String): Clip = Clip.fromJson(api.generateClip(validId(id)) as JSONObject)
    suspend fun clip(id: String): Clip = Clip.fromJson(api.json("api/archive/exports/${validId(id)}") as JSONObject)
    suspend fun manifest(id: String) { api.prepareClipManifest(validId(id)) }
    suspend fun limits(): Long = (api.json("api/archive/exports/limits") as JSONObject).getLong("max_duration_seconds")
    suspend fun downloadToken(id: String): JSONObject = api.json("api/archive/exports/${validId(id)}/download-token", "POST", JSONObject()) as JSONObject
    suspend fun deleteClip(id: String) { api.json("api/archive/exports/${validId(id)}?confirm=true", "DELETE") }
    private fun validId(value: String) = value.also { require(it.matches(Regex("[a-fA-F0-9-]{1,64}"))) }
}

fun encode(value: String): String = URLEncoder.encode(value, Charsets.UTF_8.name())
fun parseTime(value: String, zone: ZoneId): Instant? = try { OffsetDateTime.parse(value).toInstant() }
    catch (_: Exception) { try { LocalDateTime.parse(value).atZone(zone).toInstant() } catch (_: Exception) { null } }
fun zoneLabel(zone: ZoneId): String = zone.rules.getOffset(Instant.now()).id.let { if (it == "Z") "UTC" else "UTC$it" }
