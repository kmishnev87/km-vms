package ru.kmvms.mobile.ui.recording

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.events.CameraEventKind

internal val recordingProfiles = listOf("economical", "balanced", "full_quality")
internal val supportedKinds = CameraEventKind.entries.filterNot { it == CameraEventKind.OTHER }
internal val allRecordingKinds = supportedKinds.map { it.code }.toSet()
internal val aiKinds = setOf("person", "vehicle", "animal", "motion")
private val aiDefaults = aiKinds - "motion"
internal fun allowedKinds(source: String) = if (source == "ai") aiKinds else allRecordingKinds

private fun JSONObject.text(name: String): String? = (opt(name) as? String)?.takeIf { it.isNotEmpty() }
private fun JSONObject.positiveId(name: String): Long? = optLong(name).takeIf { it > 0 }
private fun JSONArray?.strings(): List<String>? =
    this?.let { values -> (0 until values.length()).mapNotNull { values.opt(it) as? String } }

internal data class RecordingDraft(
    val mode: String, val source: String, val profile: String, val seconds: String, val minutes: Int,
    val sourceId: Long?, val kinds: Set<String>, val rules: List<String>,
    val aiRules: AiRules? = null, val aiRulesEdited: Boolean = false, val alertQuietMinutes: Int? = null,
) {
    val hasAlertInterval: Boolean get() = mode == "continuous" && source != "none" && alertQuietMinutes != null

    fun withMode(value: String, camera: RecordingCamera? = null): RecordingDraft = if (value == mode) this else copy(
        mode = value, source = when {
            value == "events" -> "camera"
            value == "continuous" && mode != "linked" -> source
            else -> "none"
        },
        profile = if (value == "continuous") "none" else profile.takeIf { it in recordingProfiles } ?: "economical",
        seconds = if (value == "continuous") "0" else seconds, sourceId = null, rules = emptyList(),
    ).let { draft ->
        if (value == "events" && (source == "ai" || camera?.let { it.aiAvailable && !it.canUseEvents } == true))
            draft.withSource("ai", retainScene = source == "ai") else draft
    }

    fun profiles() = if (source == "ai") recordingProfiles.drop(1) else recordingProfiles

    fun withSource(value: String, retainScene: Boolean = source == "ai"): RecordingDraft {
        val fallback = value == "ai" && mode == "events" && profile !in recordingProfiles.drop(1)
        val selected = kinds.intersect(allowedKinds(value)).let { if (value == "ai" && !retainScene) it - "motion" else it }
        return copy(source = value, rules = emptyList(), profile = if (fallback) "balanced" else profile,
            seconds = if (fallback) "30" else seconds,
            kinds = selected.ifEmpty { if (value == "ai") aiDefaults else allRecordingKinds })
    }

    fun withProfile(value: String) = copy(profile = value, seconds = when {
        value == "economical" -> "0"
        profile == "economical" -> "30"
        else -> seconds
    })

    fun payload(revision: Int) = JSONObject()
        .put("expected_revision", revision)
        .apply { if (hasAlertInterval) put("alert_quiet_minutes", alertQuietMinutes) }
        .put("policy", JSONObject()
            .put("mode", mode)
            .put("source", if (mode == "linked") "none" else source)
            .put("profile", if (mode == "continuous") "none" else profile)
            .put("prebuffer_seconds", if (mode == "continuous" || profile == "economical") 0 else seconds.toInt())
            .put("postrecord_minutes", if (mode == "events") minutes else if (hasAlertInterval) alertQuietMinutes else 1)
            .put("linked_camera_id", if (mode == "linked") sourceId else JSONObject.NULL)
            .put("event_kinds", JSONArray(kinds.ifEmpty { if (source == "ai") aiDefaults else allRecordingKinds }.toList()))
            .put("rule_ids", JSONArray(if (mode == "events") rules else emptyList<String>()))
            .apply { if (aiRules != null || aiRulesEdited) put("ai_rules", aiRules?.json() ?: JSONObject.NULL) })

    fun isValid(camera: RecordingCamera, sources: List<RecordingCamera>): Boolean {
        if (source == "ai" && (!camera.aiAvailable || rules.isNotEmpty()) || source == "camera" && !camera.canUseEvents) return false
        if (mode == "linked" && sources.none { it.id == sourceId }) return false
        if (source == "ai" && aiRules?.issue() != null) return false
        if (source != "none" && !(source == "ai" && aiRules != null) && kinds.isEmpty()) return false
        if (mode != "continuous" && profile !in profiles()) return false
        return mode == "continuous" || profile == "economical" || seconds.toIntOrNull()?.let { it in 0..120 } == true
    }
}

internal data class RecordingIncident(val reason: String?, val attempts: Int, val last: String?, val next: String?)
internal data class LocalAiStatus(val phase: String?, val reason: String?, val model: String?, val device: String?,
                                  val version: String?, val runtime: String?, val rulesView: String? = null)
internal data class RecordingCamera(
    val id: Long, val name: String, val enabled: Boolean, val protocol: String, val receivingEvents: Boolean,
    val revision: Int, val draft: RecordingDraft, val state: String?, val eventDriver: Boolean, val legacyMode: String?,
    val emergencyRequested: Boolean, val emergencyRecording: Boolean, val sourceStatus: String?,
    val sourceIntent: String?, val sourceConfirmed: Boolean,
    val bufferStatus: String?, val bufferedSeconds: Int, val bufferLimited: Boolean,
    val incident: RecordingIncident?, val retryCameraId: Long?, val aiAvailable: Boolean, val ai: LocalAiStatus?,
    val eventsSourceAvailable: Boolean = receivingEvents,
) {
    val canUseEvents: Boolean get() = protocol == "onvif" && eventsSourceAvailable
    companion object {
        fun fromJson(row: JSONObject): RecordingCamera {
            val policy = row.getJSONObject("policy")
            val source = policy.optString("source", "none")
            val runtime = row.optJSONObject("runtime") ?: JSONObject()
            val link = runtime.optJSONObject("link") ?: JSONObject()
            val incident = runtime.optJSONObject("incident")?.let {
                RecordingIncident(it.text("reason"), it.optInt("failed_attempts"), it.text("last_failure_at"), it.text("next_attempt_at"))
            }
            return RecordingCamera(row.getLong("camera_id"), row.getString("name"), row.getBoolean("recording_enabled"),
                row.optString("protocol"), row.optBoolean("events_enabled"), policy.getInt("revision"),
                RecordingDraft(policy.optString("mode", "continuous"), source,
                    policy.optString("profile", "none"), policy.optInt("prebuffer_seconds").toString(),
                    if (policy.optString("mode") == "events") policy.optInt("postrecord_minutes", 1) else 1,
                    policy.positiveId("linked_camera_id"),
                    (policy.optJSONArray("event_kinds").strings()?.toSet() ?: allowedKinds(source)).intersect(allowedKinds(source)),
                    policy.optJSONArray("rule_ids").strings().orEmpty(), policy.optJSONObject("ai_rules")?.let(AiRules::read),
                    alertQuietMinutes = row.optInt("alert_quiet_minutes", -1).takeIf { it in 1..5 }),
                runtime.text("actual_status"), runtime.optBoolean("event_recording_available"), runtime.text("legacy_recording_mode"),
                runtime.optBoolean("emergency_requested"), runtime.optBoolean("emergency_recording"), runtime.text("source_status"),
                link.text("source_intent"), link.optBoolean("source_recording_confirmed"),
                runtime.text("prebuffer_status"), runtime.optInt("prebuffer_seconds_available"),
                runtime.optBoolean("prebuffer_capacity_limited"), incident, runtime.positiveId("retry_camera_id"),
                runtime.optBoolean("ai_available"), runtime.optJSONObject("ai")?.let {
                    LocalAiStatus(it.text("phase"), it.text("reason"), it.text("model"), it.text("device"),
                        it.text("version"), it.text("runtime"), it.text("rules_view"))
                }, row.optBoolean("camera_events_available", row.optBoolean("events_enabled")))
        }
    }
}

private fun ultimate(rows: List<RecordingCamera>, node: RecordingCamera?, dependent: Long): RecordingCamera? {
    val byId = rows.associateBy { it.id }
    val seen = mutableSetOf(dependent)
    var current = node
    while (current != null) {
        if (!seen.add(current.id)) return null
        if (current.draft.mode != "linked") return current
        current = byId[current.draft.sourceId ?: return null]
    }
    return null
}

internal fun linkedSources(rows: List<RecordingCamera>, dependent: Long) =
    rows.filter { ultimate(rows, it, dependent) != null }

internal fun sourceIsContinuous(rows: List<RecordingCamera>, source: Long?, dependent: Long): Boolean =
    ultimate(rows, rows.find { it.id == source }, dependent)?.let { !it.eventDriver && it.legacyMode == "always" } == true

internal class RecordingCenterApi(private val api: NasApi) {
    private val base = "api/ai-recording"
    suspend fun cameras(): List<RecordingCamera> {
        val list = (api.json("$base/cameras") as JSONObject).getJSONArray("cameras")
        return (0 until list.length()).map { RecordingCamera.fromJson(list.getJSONObject(it)) }
    }
    suspend fun emergency(): Boolean = (api.json("$base/emergency") as JSONObject).getBoolean("enabled")
    suspend fun setEmergency(previous: Boolean): Boolean =
        (api.json("$base/emergency", "PUT", JSONObject().put("expected_enabled", previous).put("enabled", !previous)) as JSONObject)
            .getBoolean("enabled")
    suspend fun save(camera: RecordingCamera, draft: RecordingDraft): RecordingCamera =
        RecordingCamera.fromJson(api.json("$base/cameras/${camera.id}", "PUT", draft.payload(camera.revision)) as JSONObject)
    suspend fun retry(id: Long) { api.json("$base/cameras/$id/retry", "POST", JSONObject()) }
    suspend fun ruleFrame(id: Long, profile: String): AiEditorFrame {
        val value = api.json("$base/cameras/$id/rule-frame?profile=$profile") as JSONObject
        return withContext(Dispatchers.Default) {
            val image = value.getString("image")
            val width = value.getInt("width")
            val height = value.getInt("height")
            val reference = value.getString("reference")
            require(width in 1..800 && height in 1..600 && image.length in 1..355000 && reference.length in 1..1024)
            val raw = Base64.decode(image, Base64.DEFAULT)
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeByteArray(raw, 0, raw.size, bounds)
            require(bounds.outWidth == width && bounds.outHeight == height)
            val bitmap = requireNotNull(BitmapFactory.decodeByteArray(raw, 0, raw.size))
            AiEditorFrame(bitmap, reference, value.optBoolean("matching_saved", true))
        }
    }
}

internal data class AiEditorFrame(val image: Bitmap, val reference: String, val matchingSaved: Boolean)
