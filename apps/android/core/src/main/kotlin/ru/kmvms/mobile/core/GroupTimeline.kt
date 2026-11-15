package ru.kmvms.mobile.core

import kotlin.math.abs

enum class TimelinePhase { RESOLVING, BUFFERING, READY, EMPTY, FAILED }

data class TimelineSample(
    val cameraId: Long,
    val phase: TimelinePhase,
    val positionMillis: Long? = null,
    val bufferedAheadMillis: Long = 0,
    val remainingMillis: Long = Long.MAX_VALUE,
    val bufferCapacityMillis: Long = Long.MAX_VALUE,
)

data class TimelineStep(
    val positionMillis: Long,
    val running: Boolean,
    val waiting: Set<Long>,
    val seekToMillis: Long? = null,
)

/**
 * A single clock and readiness barrier for an archive group. A deadline can
 * change the UI, but never silently remove a slow/failed recording from the
 * barrier. EMPTY means confirmed absence, not an error or an unfinished lookup.
 */
class GroupTimeline(initialPositionMillis: Long, private val toleranceMillis: Long = 500) {
    var positionMillis: Long = initialPositionMillis
        private set
    var wantsPlayback: Boolean = false
        private set
    var running: Boolean = false
        private set
    private var members: Set<Long> = emptySet()
    private var foreground = true
    private val buffers=ArchiveBufferPolicy()
    val bufferReserveMillis get()=buffers.reserveMillis

    fun prepare(position: Long, cameraIds: Set<Long>, resume: Boolean = wantsPlayback, resetBuffer: Boolean = true) {
        if(resetBuffer)buffers.reset()
        positionMillis = position
        members = cameraIds.toSet()
        wantsPlayback = resume
        running = false
    }

    fun play(value: Boolean) {
        wantsPlayback = value
        if (!value) running = false
    }

    fun foreground(value: Boolean) {
        foreground = value
        if (!value) running = false
    }

    fun step(samples: List<TimelineSample>, elapsedMillis: Long, speed: Float): TimelineStep {
        require(speed.isFinite() && speed > 0)
        val byId = samples.associateBy { it.cameraId }
        val current = members.map { byId[it] ?: TimelineSample(it, TimelinePhase.RESOLVING) }
        val waiting = current.filter { it.phase != TimelinePhase.EMPTY &&
            (it.phase != TimelinePhase.READY || it.positionMillis == null) }
            .map { it.cameraId }.toSet()
        val ready = current.filter { it.phase == TimelinePhase.READY }.mapNotNull { it.positionMillis }

        if (!wantsPlayback || !foreground || members.isEmpty()) {
            running = false
            return TimelineStep(positionMillis, false, waiting)
        }
        if (waiting.isNotEmpty()) {
            val wasRunning = running
            if(wasRunning && current.any{it.phase==TimelinePhase.BUFFERING && it.positionMillis!=null})buffers.starved()
            if (wasRunning) {
                current.filter { it.phase in setOf(TimelinePhase.READY, TimelinePhase.BUFFERING) }
                    .mapNotNull { it.positionMillis }.minOrNull()?.let { positionMillis = it }
            }
            running = false
            return TimelineStep(positionMillis, false, waiting, if (wasRunning) positionMillis else null)
        }
        if(!running) {
            val filling=current.filter { it.phase==TimelinePhase.READY &&
                it.bufferedAheadMillis<buffers.required(speed,it.remainingMillis,it.bufferCapacityMillis) }
                .map{it.cameraId}.toSet()
            if(filling.isNotEmpty())return TimelineStep(positionMillis,false,filling)
        }
        if (!running && ready.any { abs(it - positionMillis) > toleranceMillis }) {
            return TimelineStep(positionMillis, false, emptySet(), positionMillis)
        }
        if (ready.isNotEmpty() && ready.max() - ready.min() > toleranceMillis) {
            positionMillis = ready.min()
            running = false
            return TimelineStep(positionMillis, false, emptySet(), positionMillis)
        }

        // No recording is different from stalled media. Cross an empty interval
        // with the common clock, so a later recording can join at its actual time.
        positionMillis = if (ready.isEmpty()) {
            positionMillis + (elapsedMillis.coerceIn(0, 500) * speed).toLong()
        } else maxOf(positionMillis, ready.min())
        running = true
        buffers.progressed(elapsedMillis)
        return TimelineStep(positionMillis, true, emptySet())
    }
}
