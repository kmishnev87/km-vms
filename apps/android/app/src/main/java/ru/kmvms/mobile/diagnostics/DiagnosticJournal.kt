package ru.kmvms.mobile.diagnostics

import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.util.UUID

/** Typed fields only: no exception text, addresses, headers or packet bodies. */
enum class DiagnosticCode {
    APP_STARTED, PUSH_RECEIVED, PUSH_NOT_DECRYPTED, PUSH_NO_BINDING, PUSH_DISABLED,
    PUSH_NO_PROFILE, PUSH_NO_SESSION, PUSH_PARSE_FAILED, PUSH_PARSED,
    PUSH_PERMISSION_BLOCKED, PUSH_CHANNEL_BLOCKED, PUSH_DUPLICATE, PUSH_LIVE_SUPPRESSED,
    PUSH_POSTED, PUSH_POST_FAILED, PUSH_STATE, PUSH_UPLOAD_FAILED, API_FAILED,
    LIVE_FAILED, ARCHIVE_FAILED, UPDATE_FAILED, REPORT_ACCEPTED, REPORT_FAILED
}
enum class DiagnosticArea { APP, PUSH, AUTH, CAMERAS, LIVE, ARCHIVE, EVENTS, SETTINGS, UPDATE, SUPPORT, OTHER }

data class DiagnosticEntry(val time: Long, val code: DiagnosticCode, val area: DiagnosticArea = DiagnosticArea.APP,
    val profile: String? = null, val event: String? = null, val camera: Long? = null, val number: Int? = null) {
    fun json() = JSONObject().put("time", time).put("code", code.name).put("area", area.name).apply {
        profile?.takeIf { it.matches(Regex("[0-9a-f]{16}")) }?.let { put("profile", it) }
        event?.takeIf { runCatching { UUID.fromString(it).toString() == it }.getOrDefault(false) }?.let { put("event", it) }
        camera?.takeIf { it > 0 }?.let { put("camera", it) }
        number?.let { put("number", it) }
    }
    companion object {
        fun profileKey(id: String): String = MessageDigest.getInstance("SHA-256").digest(id.toByteArray())
            .take(8).joinToString("") { "%02x".format(it) }
        fun parse(v: JSONObject) = DiagnosticEntry(v.getLong("time"), DiagnosticCode.valueOf(v.getString("code")),
            DiagnosticArea.valueOf(v.getString("area")), v.optString("profile").ifBlank { null }, v.optString("event").ifBlank { null },
            if (v.has("camera")) v.getLong("camera") else null, if (v.has("number")) v.getInt("number") else null)
    }
}

/** Private event-driven journal, <=5 MiB and <=7 days. No timer or ZIP. */
class DiagnosticJournal(private val directory: File, private val maxBytes: Long = 5L * 1024 * 1024,
    private val retentionMs: Long = 7L * 86400000, private val clock: () -> Long = System::currentTimeMillis) {
    private val names = Regex("events-(\\d{13})-[0-9a-f]{8}\\.jsonl")
    private fun files() = directory.listFiles().orEmpty().filter { it.isFile && names.matches(it.name) }.sortedBy { it.name }
    private fun created(f: File) = names.matchEntire(f.name)!!.groupValues[1].toLong()
    private fun prune(): List<File> {
        directory.mkdirs()
        return files().filter { f -> if (created(f) < clock() - retentionMs) { f.delete(); false } else true }
    }
    @Synchronized fun append(entry: DiagnosticEntry) {
        val bytes = (entry.json().toString() + "\n").toByteArray(Charsets.UTF_8)
        if (bytes.size > 1024 || bytes.size > maxBytes) return
        val retained = prune().toMutableList()
        var used = retained.sumOf { it.length() }
        while (used + bytes.size > maxBytes && retained.isNotEmpty()) {
            val oldest = retained.removeAt(0); val length = oldest.length()
            if (!oldest.delete()) return
            used -= length
        }
        val now = clock()
        val target = retained.lastOrNull()?.takeIf { it.length() + bytes.size <= minOf(256 * 1024L, maxBytes) && now - created(it) < 86400000 }
            ?: File(directory, "events-$now-${UUID.randomUUID().toString().take(8)}.jsonl")
        target.appendBytes(bytes)
    }
    @Synchronized fun snapshot(limit: Int = 2000): List<JSONObject> {
        val cutoff = clock() - retentionMs
        val retained = ArrayDeque<JSONObject>()
        val maximum = limit.coerceIn(1, 2000)
        prune().forEach { file -> file.useLines { lines -> lines.forEach { line ->
            val entry = if (line.length > 1024) null else runCatching { DiagnosticEntry.parse(JSONObject(line)) }.getOrNull()
            if (entry != null && entry.time >= cutoff) {
                if (retained.size == maximum) retained.removeFirst()
                retained.addLast(entry.json())
            }
        } } }
        return retained.toList()
    }
}
