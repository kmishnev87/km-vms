package ru.kmvms.mobile.data

import org.json.JSONObject
import java.util.Base64
import kotlin.math.abs

data class LiveAiShape(val points: List<Pair<Int, Int>>, val line: Boolean, val mask: Boolean)
data class LiveAiReference(val ratio: Int, val edges: String, val mode: String = "ptz") {
    internal fun bytes(): ByteArray? = runCatching { Base64.getUrlDecoder().decode(edges) }
        .getOrNull()?.takeIf { it.size == 112 && ratio in 50..20000 }
    internal fun geometryMatches(aspect: Float) = aspect.isFinite() && aspect > 0f &&
        abs(aspect * 1000.0 - ratio) <= maxOf(16.0, ratio * .018)
}
data class LiveAiGeometry(val shapes: List<LiveAiShape>, val view: LiveAiReference?) {
    companion object {
        internal fun read(json: JSONObject?): LiveAiGeometry? {
            val list = json?.optJSONArray("shapes") ?: return null
            val shapes = (0 until minOf(12, list.length())).mapNotNull { i ->
                val shape = list.optJSONObject(i) ?: return@mapNotNull null
                val line = shape.optBoolean("line")
                val array = shape.optJSONArray("points") ?: return@mapNotNull null
                if (array.length() !in (if (line) 2..2 else 3..10)) return@mapNotNull null
                val points = (0 until array.length()).map { p ->
                    val point = array.optJSONArray(p) ?: return@mapNotNull null
                    val x = point.optInt(0, -1)
                    val y = point.optInt(1, -1)
                    if (x !in 0..1000 || y !in 0..1000) return@mapNotNull null
                    x to y
                }
                LiveAiShape(points, line, shape.optBoolean("mask"))
            }
            if (shapes.isEmpty()) return null
            val reference = json.optJSONObject("view")?.let {
                val mode = it.optString("mode").takeIf { value -> value == "fixed" || value == "unknown" } ?: "ptz"
                LiveAiReference(it.optInt("ratio"), it.optString("edges"), mode)
            }
                ?.takeIf { it.bytes() != null }
            return LiveAiGeometry(shapes, reference)
        }
    }
}

private data class EdgeAgreement(val known: Int, val light: Double, val dark: Double, val coverage: Double,
                                 val structure: Double, val spread: Int)

// Keep the display matcher in sync with web/lib/aiLiveView.js; Recorder uses
// its own stricter same-stream check for the rules themselves.
internal fun aiViewScore(ratio: Int, edges: ByteArray, luma: IntArray, aspect: Float): Double {
    if (edges.size != 112 || luma.size != 240 || !aspect.isFinite() || aspect <= 0f) return 0.0
    val difference = abs(aspect * 1000.0 - ratio)
    val sameAspect = difference <= maxOf(16.0, ratio * .018)
    if (difference > ratio * .30) return 0.0
    fun compare(dx: Int, dy: Int): EdgeAgreement {
        var index = 0
        var known = 0; var light = 0; var dark = 0; var matched = 0; var extra = 0; var blank = 0
        val quadrants = IntArray(4)
        fun edge(x: Int, y: Int, vertical: Boolean) {
            val dir = (edges[index shr 2].toInt() shr ((index++ and 3)*2)) and 3
            val sx = x+dx; val sy = y+dy
            if (sx !in 0 until (if (vertical) 20 else 19) || sy !in 0 until (if (vertical) 11 else 12)) return
            val i = sy*20+sx
            val delta = luma[i+(if (vertical) 20 else 1)] - luma[i]
            val strong = abs(delta) >= 8
            if (dir == 0) { blank++; if (strong) extra++; return }
            known++
            val signed = if (dir == 1) delta else -delta
            if (signed >= 6) light++
            if (signed <= -6) dark++
            if (strong) {
                matched++
                quadrants[(if (x >= 10) 1 else 0) + (if (y >= 6) 2 else 0)]++
            }
        }
        for (y in 0..11) for (x in 0..18) edge(x, y, false)
        for (y in 0..10) for (x in 0..19) edge(x, y, true)
        val coverage = matched.toDouble()/known
        return EdgeAgreement(known, light.toDouble()/known, dark.toDouble()/known, coverage,
            coverage - if (blank > 0) extra.toDouble()/blank else 1.0, quadrants.count { it >= 4 })
    }
    val still = compare(0, 0)
    if (still.known < 24) return 0.0
    if (sameAspect && still.light >= .77) return still.light
    val polarity = if (sameAspect) still.dark else maxOf(still.light, still.dark)
    val signedCandidate = polarity >= .86
    val structureCandidate = still.coverage >= .70 && still.structure >= .22
    if (still.spread >= 3 && (signedCandidate || structureCandidate)) {
        var shiftedSigned = 0.0; var shiftedStructure = -1.0
        for (y in -1..1) for (x in -1..1) if (x != 0 || y != 0) {
            val shifted = compare(x, y)
            if (shifted.known >= 24) {
                shiftedSigned = maxOf(shiftedSigned, shifted.light, shifted.dark)
                shiftedStructure = maxOf(shiftedStructure, shifted.structure)
            }
        }
        if (signedCandidate && polarity >= shiftedSigned + .12 ||
            structureCandidate && still.structure >= shiftedStructure + .12) return .8
    }
    return if (sameAspect) still.light else 0.0
}

internal data class AiViewStability(val good: Int = 0, val bad: Int = 0, val visible: Boolean = false) {
    fun sample(score: Double): AiViewStability = if (score >= .77) {
        val n = minOf(3, good + 1)
        AiViewStability(n, 0, visible || n >= 3)
    } else {
        val n = minOf(2, bad + 1)
        AiViewStability(0, n, visible && score >= .48 && n < 2)
    }
}
