package ru.kmvms.mobile.ui.recording

import org.json.JSONArray
import org.json.JSONObject
import ru.kmvms.mobile.R
import kotlin.math.abs
import java.util.UUID

internal val aiRuleKinds = listOf("presence", "enter", "exit", "crossing", "dwell", "scene")
internal val aiRuleClasses = listOf("person", "vehicle", "animal")

internal data class AiPoint(val x: Int, val y: Int) {
    fun json() = JSONArray(listOf(x, y))
}
private fun shape(json: JSONArray?): List<AiPoint> = if (json == null) emptyList() else
    (0 until json.length()).map { index -> json.getJSONArray(index).let { AiPoint(it.getInt(0), it.getInt(1)) } }
private fun JSONArray.stringSet() = (0 until length()).map { getString(it) }.toSet()

internal data class AiSchedule(val days: Set<Int> = (1..7).toSet(), val start: Int = 0, val end: Int = 0) {
    fun json() = JSONObject().put("days", JSONArray(days.sorted())).put("start", start).put("end", end)
    companion object {
        fun read(row: JSONObject): AiSchedule {
            val days = row.getJSONArray("days")
            return AiSchedule((0 until days.length()).map { days.getInt(it) }.toSet(), row.getInt("start"), row.getInt("end"))
        }
    }
}

internal data class AiRule(
    val id: String = UUID.randomUUID().toString(), val name: String = "", val kind: String = "presence",
    val classes: Set<String> = setOf("person"), val points: List<AiPoint> = emptyList(), val direction: String = "both",
    val confidence: Int = 50, val confirm: Int = 0, val release: Int = 2, val dwell: Int = 10,
    val schedule: AiSchedule? = null,
    val sceneObjects: Boolean = false,
) {
    fun json() = JSONObject().put("id", id).put("name", name).put("kind", kind).put("classes", JSONArray(classes.toList()))
        .put("points", JSONArray(points.map { it.json() })).put("direction", direction).put("confidence", confidence)
        .put("confirm_seconds", confirm).put("release_seconds", release).put("dwell_seconds", dwell)
        // Old NAS releases still read the legacy action fields.
        .put("schedule", schedule?.json() ?: JSONObject.NULL).put("record", true).put("notify", true)
        .apply { if (sceneObjects && kind == "scene") put("scene_objects", true) }
    companion object {
        fun read(row: JSONObject) = AiRule(row.getString("id"), row.optString("name"), row.getString("kind"),
            row.getJSONArray("classes").stringSet(), shape(row.optJSONArray("points")), row.optString("direction", "both"),
            row.optInt("confidence", 50), row.optInt("confirm_seconds"), row.optInt("release_seconds", 2),
            row.optInt("dwell_seconds", 10), row.optJSONObject("schedule")?.let(AiSchedule::read), row.optBoolean("scene_objects"))
    }
}

internal data class AiRules(
    val rules: List<AiRule> = emptyList(), val exclusions: List<List<AiPoint>> = emptyList(), val reference: String? = null,
) {
    fun json() = JSONObject().put("rules", JSONArray(rules.map { it.json() }))
        .put("exclusions", JSONArray(exclusions.map { row -> JSONArray(row.map { it.json() }) }))
        .put("reference", reference ?: JSONObject.NULL)
    fun issue(): Int? {
        if (rules.isEmpty()) return R.string.ai_rule_add_prompt
        if (reference.isNullOrBlank() && (exclusions.isNotEmpty() || rules.any { it.points.isNotEmpty() })) return R.string.ai_rule_capture_first
        if (exclusions.any { !validPolygon(it) }) return R.string.ai_rule_polygon_invalid
        for (rule in rules) {
            if ((rule.kind != "scene" || rule.sceneObjects) && rule.classes.isEmpty()) return R.string.ai_rule_class_required
            if (rule.schedule?.days?.isEmpty() == true) return R.string.ai_rule_days_required
            if (rule.kind == "crossing") {
                if (rule.points.size != 2 || rule.points.let { (a, b) ->
                    (a.x-b.x)*(a.x-b.x) + (a.y-b.y)*(a.y-b.y) < 2500 }) return R.string.ai_rule_line_invalid
            } else if ((rule.kind in setOf("enter", "exit", "dwell") || rule.points.isNotEmpty()) && !validPolygon(rule.points))
                return R.string.ai_rule_polygon_invalid
            if (rule.confidence !in 35..95 || rule.confirm !in 0..20 || rule.release !in 0..15 || rule.dwell !in 3..300)
                return R.string.ai_rule_range
        }
        return null
    }
    companion object {
        fun read(row: JSONObject): AiRules {
            val items = row.getJSONArray("rules")
            val masks = row.optJSONArray("exclusions") ?: JSONArray()
            return AiRules((0 until items.length()).map { AiRule.read(items.getJSONObject(it)) },
                (0 until masks.length()).map { shape(masks.getJSONArray(it)) }, row.opt("reference") as? String)
        }
    }
}

private fun validPolygon(points: List<AiPoint>): Boolean {
    if (points.size < 3 || points.toSet().size != points.size) return false
    val edges = points.zip(points.drop(1) + points.first())
    fun orientation(a: AiPoint, b: AiPoint, c: AiPoint): Long =
        (b.x-a.x).toLong()*(c.y-a.y) - (b.y-a.y).toLong()*(c.x-a.x)
    fun intersect(a: AiPoint, b: AiPoint, c: AiPoint, d: AiPoint) =
        maxOf(minOf(a.x,b.x),minOf(c.x,d.x)) <= minOf(maxOf(a.x,b.x),maxOf(c.x,d.x)) &&
        maxOf(minOf(a.y,b.y),minOf(c.y,d.y)) <= minOf(maxOf(a.y,b.y),maxOf(c.y,d.y)) &&
        orientation(a,b,c)*orientation(a,b,d) <= 0 && orientation(c,d,a)*orientation(c,d,b) <= 0
    return abs(edges.sumOf { (a,b) -> a.x*b.y-a.y*b.x }) >= 800 &&
        edges.indices.none { i -> (i+2 until edges.size).any { j ->
            (i > 0 || j < edges.lastIndex) && intersect(edges[i].first, edges[i].second, edges[j].first, edges[j].second)
        } }
}
