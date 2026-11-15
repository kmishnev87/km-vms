package ru.kmvms.mobile.core

import kotlin.math.ceil
import kotlin.math.roundToLong

/** Pure geometry, shared by drawing and gestures. A gesture does not perform IO. */
data class TimelineViewport(val dayStart: Long, val dayEnd: Long, val start: Long, val span: Long) {
    val end get() = start + span
    fun fraction(time: Long): Float = (time - start).toDouble().div(span).toFloat()
    fun time(fraction: Float): Long = (start + (span * fraction.coerceIn(0f, 1f).toDouble()).roundToLong())
        .coerceIn(dayStart, dayEnd - 1)
    fun centred(time: Long, duration: Long = span): TimelineViewport = around(dayStart, dayEnd, time, duration)
    fun shifted(delta: Long): TimelineViewport = copy(start=(start + delta).coerceIn(dayStart, dayEnd - span))
    fun ticks(maxLabels: Int): List<Long> {
        val minutes = listOf(1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440)
        val minimum = span.toDouble() / maxLabels.coerceAtLeast(2)
        val step = minutes.firstOrNull { it * 60_000L >= minimum }?.times(60_000L) ?: 86_400_000L
        val first = dayStart + ceil((start - dayStart).toDouble() / step).toLong() * step
        return generateSequence(first) { it + step }.takeWhile { it <= end }.toList()
    }
    /** Painted two-pixel fragments must select their real interval, never an adjacent gap. */
    fun hit(fraction: Float, width: Float, intervals: List<Pair<Long, Long>>): Long {
        val x = fraction.coerceIn(0f, 1f) * width
        val hit = intervals.firstOrNull { (from, to) ->
            val left = this.fraction(from).coerceIn(0f, 1f) * width
            val right = this.fraction(to).coerceIn(0f, 1f) * width
            to > start && from < end && x >= left && x <= maxOf(right, left + 2f)
        } ?: return time(fraction)
        return time(fraction).coerceIn(maxOf(hit.first, start), minOf(hit.second - 1, end - 1))
    }
    companion object {
        val scales = listOf(24 * 60, 12 * 60, 6 * 60, 3 * 60, 60, 30, 10)
        fun around(dayStart: Long, dayEnd: Long, time: Long, duration: Long): TimelineViewport {
            require(dayEnd > dayStart)
            val span = duration.coerceIn(1, dayEnd - dayStart)
            return TimelineViewport(dayStart, dayEnd, (time - span / 2).coerceIn(dayStart, dayEnd - span), span)
        }
    }
}
