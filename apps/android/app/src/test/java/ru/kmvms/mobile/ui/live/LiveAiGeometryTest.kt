package ru.kmvms.mobile.ui.live

import org.junit.Assert.assertTrue
import org.junit.Assert.assertFalse
import org.junit.Test
import ru.kmvms.mobile.data.AiViewStability
import ru.kmvms.mobile.data.LiveAiReference
import ru.kmvms.mobile.data.aiViewScore

class LiveAiGeometryTest {
    @Test fun fixedViewportUsesGeometryWithoutRequiringAnyPixelSample() {
        val view = LiveAiReference(2202, "", "fixed")
        assertTrue(view.geometryMatches(2048f / 928f))
        assertFalse(view.geometryMatches(640f / 360f))
    }

    @Test fun stationaryInfraredAndResizedStreamsAreNotMistakenForAPtzTurn() {
        val luma = IntArray(240) { i ->
            val block = (i % 20) / 4 + 5 * ((i / 20) / 3)
            25 + (block * 59 + block * block * 17) % 199
        }
        val edges = ByteArray(112)
        var p = 0
        fun sign(delta: Int) {
            val value = if (delta >= 12) 1 else if (delta <= -12) 2 else 0
            edges[p shr 2] = (edges[p shr 2].toInt() or (value shl (2 * (p and 3)))).toByte()
            p++
        }
        for (y in 0..11) for (x in 0..18) sign(luma[y*20+x+1] - luma[y*20+x])
        for (i in 0..219) sign(luma[i+20] - luma[i])
        val infrared = IntArray(240) { 218 - kotlin.math.round(luma[it] * .6).toInt() }
        val shifted = IntArray(240) { if (it % 20 < 18) infrared[it+2] else 35 }
        val night = aiViewScore(2202, edges, infrared, 640f/360f)
        val moved = aiViewScore(2202, edges, shifted, 640f/360f)
        var stability = AiViewStability()
        repeat(3) { stability = stability.sample(night) }
        assertTrue(stability.visible)
        assertTrue(moved < .48 && !stability.sample(moved).visible)
    }
}
