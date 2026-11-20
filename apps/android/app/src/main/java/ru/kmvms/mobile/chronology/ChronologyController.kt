package ru.kmvms.mobile.chronology

import android.content.Context
import android.os.SystemClock
import androidx.media3.common.util.UnstableApi
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import ru.kmvms.mobile.core.GroupTimeline
import ru.kmvms.mobile.core.TimelinePhase
import ru.kmvms.mobile.core.TimelineSample
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZoneOffset

data class ChronologyState(
    val position: Long,
    val selected: List<Long> = emptyList(),
    val phases: Map<Long, TimelinePhase> = emptyMap(),
    val wantsPlay: Boolean = false,
    val running: Boolean = false,
    val waiting: Set<Long> = emptySet(),
    val speed: Float = 1f,
    val zone: ZoneId = ZoneOffset.UTC,
    val day: LocalDate = LocalDate.now(ZoneOffset.UTC),
    val ranges: Map<Long, List<TimelineRange>> = emptyMap(),
    val loadingRanges: Boolean = false,
    val failures: Int = 0,
    val findingNext: Boolean = false,
    val noNextRecording: Boolean = false,
)

/** Foreground-only owner of all selected archive players and their common clock. */
@UnstableApi
class ChronologyController(private val context: Context, private val api: ChronologyApi,
    initialPosition: Long = System.currentTimeMillis() - 5 * 60_000,
    initialZone: ZoneId = ZoneOffset.UTC,
    initialDay: LocalDate = Instant.ofEpochMilli(initialPosition).atZone(initialZone).toLocalDate()) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val clock = GroupTimeline(initialPosition)
    private val mutable = MutableStateFlow(ChronologyState(clock.positionMillis, zone = initialZone, day = initialDay))
    val state = mutable.asStateFlow()
    private val tracks = linkedMapOf<Long, TimelineTrack>()
    private val lookups = mutableMapOf<Long, Job>()
    private val prefetches = mutableMapOf<Long, Job>()
    private val prefetchAttempted = mutableMapOf<Long, String>()
    private val emptyProbeAfter = mutableMapOf<Long, Long>()
    private val reads = ChronologyReadQueue(api.api.mediaClient.dispatcher.maxRequestsPerHost)
    private var rangesJob: Job? = null
    private var nextJob: Job? = null
    private var gapJob: Job? = null
    private var gapChecked = false
    private var gapLookupFailed = false
    private var gapNext: Long? = null
    private var epoch = 0L
    private var active = true
    private var rangeRequest = 0L
    private var zoneKnown = false
    private var visibleWindow:Pair<Long,Long>? = null

    init {
        scope.launch {
            var previous = SystemClock.elapsedRealtime()
            while (isActive) {
                val now = SystemClock.elapsedRealtime()
                if (active) tick(now - previous)
                previous = now
                delay(100)
            }
        }
    }

    fun track(id: Long) = tracks[id]

    fun select(ids: List<Long>) {
        val selected = ids.distinct().filter { it > 0 }
        tracks.keys.filter { it !in selected }.toList().forEach { id ->
            lookups.remove(id)?.cancel(); prefetches.remove(id)?.cancel()
            tracks.remove(id)?.close(); prefetchAttempted.remove(id); emptyProbeAfter.remove(id)
        }
        selected.forEach { id -> tracks.getOrPut(id) { TimelineTrack(context, api, id) } }
        val bufferBudget=(96*1024*1024/selected.size.coerceAtLeast(1)).coerceIn(4*1024*1024,32*1024*1024)
        tracks.values.forEach{it.bufferBudget(bufferBudget)}
        mutable.update { it.copy(selected = selected) }
        seek(clock.positionMillis)
        refreshRanges()
    }

    fun day(day: LocalDate) {
        val value = mutable.value
        val time = Instant.ofEpochMilli(clock.positionMillis).atZone(value.zone).toLocalTime()
        seek(day.atTime(time).atZone(value.zone).toInstant().toEpochMilli())
    }

    fun rangeWindow(from:Long,to:Long) {
        val before=rangeBounds()
        timelineRangeWindow(from,to,mutable.value.zone) // Validate the bounded visible request.
        visibleWindow=from to to
        if(before!=rangeBounds())refreshRanges()
    }

    private fun rangeBounds():Pair<Instant,Instant> {
        val value=mutable.value
        return visibleWindow?.let{timelineRangeWindow(it.first,it.second,value.zone)}
            ?: (value.day.atStartOfDay(value.zone).toInstant() to value.day.plusDays(1).atStartOfDay(value.zone).toInstant())
    }

    fun seek(position: Long, resetBuffer: Boolean = true) {
        val day = Instant.ofEpochMilli(position).atZone(mutable.value.zone).toLocalDate()
        val dayChanged = day != mutable.value.day
        if(visibleWindow?.let{position<it.first||position>=it.second}==true)visibleWindow=null
        if (dayChanged) mutable.update { it.copy(day = day) }
        epoch++
        nextJob?.cancel(); nextJob = null
        clearGap()
        lookups.values.forEach(Job::cancel); lookups.clear()
        prefetches.values.forEach(Job::cancel); prefetches.clear(); prefetchAttempted.clear()
        clock.prepare(position, tracks.keys.toSet(),resetBuffer=resetBuffer)
        tracks.values.forEach { track ->
            track.playback(false, mutable.value.speed)
            if (canReuseTimelineSource(track.phase(), track.canServe(position), track.ended())) track.seek(position)
            else resolve(track.cameraId, position)
        }
        mutable.update { it.copy(position = position, running = false, waiting = tracks.keys.toSet(),
            findingNext = false, noNextRecording = false) }
        if (dayChanged && zoneKnown) refreshRanges()
    }

    fun toggle() { clock.play(!clock.wantsPlayback); publishIntent() }
    fun play(value: Boolean) { clock.play(value); publishIntent() }
    fun speed(value: Float) { require(value in setOf(.25f, .5f, 1f, 2f, 4f)); mutable.update { it.copy(speed = value) } }
    fun retry() = seek(clock.positionMillis)

    fun firstRecording() {
        mutable.value.ranges.values.flatten().minOfOrNull { it.start }?.let(::seek)
    }

    fun nextRecording() {
        val selected = mutable.value.selected
        if (selected.isEmpty() || nextJob?.isActive == true) return
        val position = clock.positionMillis
        val marker = epoch
        mutable.update { it.copy(findingNext = true, noNextRecording = false) }
        nextJob = scope.launch {
            try {
                val result = api.nextRecording(selected, position)
                if (marker == epoch) {
                    if (result != null) {
                        // Do not cancel the currently executing seek job.
                        nextJob = null
                        seek(maxOf(clock.positionMillis, result))
                    } else mutable.update { it.copy(noNextRecording = true) }
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { if (marker == epoch) failure() }
            finally { if (marker == epoch) mutable.update { it.copy(findingNext = false) } }
        }
    }

    private fun clearGap() {
        gapJob?.cancel(); gapJob = null
        gapChecked = false; gapLookupFailed = false; gapNext = null
    }

    private fun findEndOfGap() {
        if (gapChecked) return
        gapChecked = true
        val marker = epoch
        val selected = mutable.value.selected
        val position = clock.positionMillis
        gapJob = scope.launch {
            try { val result = api.nextRecording(selected, position); if (marker == epoch) gapNext = result }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { if (marker == epoch) gapLookupFailed = true }
        }
    }

    fun refreshRanges() {
        rangesJob?.cancel()
        val ids = mutable.value.selected
        if (ids.isEmpty()) { mutable.update { it.copy(ranges = emptyMap(), loadingRanges = false) }; return }
        val request = ++rangeRequest
        val day = mutable.value.day
        val zone = mutable.value.zone
        val (from,to)=rangeBounds()
        mutable.update { it.copy(loadingRanges = true) }
        rangesJob = scope.launch {
            try {
                val result = api.ranges(ids, from, to)
                if (request == rangeRequest) {
                    val dayInZone = if (zoneKnown) day else Instant.ofEpochMilli(clock.positionMillis).atZone(result.zone).toLocalDate()
                    zoneKnown = true
                    mutable.update { it.copy(zone = result.zone, day = dayInZone, ranges = result.cameras) }
                    if (zone != result.zone || dayInZone != day) {
                        seek(clock.positionMillis)
                        refreshRanges()
                    }
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { if (request == rangeRequest) failure() }
            finally { if (request == rangeRequest) mutable.update { it.copy(loadingRanges = false) } }
        }
    }

    private fun resolve(id: Long, position: Long) {
        if (lookups[id]?.isActive == true || !active) return
        val track = tracks[id] ?: return
        val own = epoch
        track.resolving()
        lookups[id] = scope.launch {
            try {
                val result = reads.foreground { api.resolve(id, position) }
                if (own != epoch || tracks[id] !== track || !active) return@launch
                when (result) {
                    is TimelineLookup.Video -> { emptyProbeAfter.remove(id); track.load(result.source, position) }
                    TimelineLookup.Empty -> { track.empty(); emptyProbeAfter[id] = position + 2000 }
                    TimelineLookup.Unavailable -> track.failed()
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { if (own == epoch && tracks[id] === track) track.failed() }
        }
    }

    private fun prefetch(track: TimelineTrack, source: TimelineSource) {
        val id = track.cameraId
        if (prefetchAttempted[id] == source.key || prefetches[id]?.isActive == true || track.hasQueuedNext()) return
        prefetchAttempted[id] = source.key
        val own = epoch
        prefetches[id] = scope.launch {
            try {
                val next = reads.prefetch { api.resolve(id, source.end + 1) }
                if (own == epoch && tracks[id] === track && track.source()?.key == source.key && next is TimelineLookup.Video) {
                    track.append(next.source, source.end)
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { /* Boundary lookup remains authoritative if prefetch fails. */ }
        }
    }

    private fun tick(elapsed: Long) {
        val inGap = tracks.isNotEmpty() && tracks.values.all { it.phase() == TimelinePhase.EMPTY }
        if (inGap && clock.wantsPlayback) {
            findEndOfGap()
            val next = gapNext
            if (next != null && clock.positionMillis >= next) {
                seek(clock.positionMillis, resetBuffer = false)
                return
            }
        } else if (!inGap && gapChecked) clearGap()
        val boundary = tracks.values.filter { it.ended() && !it.hasQueuedNext() &&
            it.phase() in setOf(TimelinePhase.READY, TimelinePhase.BUFFERING) }
            .mapNotNull { it.source()?.end }.minOrNull()
        if (clock.wantsPlayback && boundary != null) {
            seek(maxOf(clock.positionMillis, boundary),resetBuffer=false)
        }
        for ((id, track) in tracks) {
            val source = track.source()
            if (track.phase() == TimelinePhase.EMPTY && clock.wantsPlayback && (!inGap || gapLookupFailed)
                && clock.positionMillis >= (emptyProbeAfter[id] ?: 0)) {
                val ranges = mutable.value.ranges[id]
                if (ranges == null || ranges.any { it.contains(clock.positionMillis) }) resolve(id, clock.positionMillis)
            }
            val lookAhead=maxOf(5000L,(clock.bufferReserveMillis*mutable.value.speed).toLong()+1000)
            if (source != null && track.phase() in setOf(TimelinePhase.READY, TimelinePhase.BUFFERING) && source.end - clock.positionMillis < lookAhead) {
                prefetch(track, source)
            }
        }
        val samples = tracks.values.map { track ->
            val sample = track.sample()
            if (sample.phase == TimelinePhase.READY && !track.canServe(clock.positionMillis)) {
                resolve(track.cameraId, clock.positionMillis)
                TimelineSample(track.cameraId, TimelinePhase.RESOLVING)
            } else sample
        }
        val step = clock.step(samples, elapsed, mutable.value.speed)
        step.seekToMillis?.let { position -> tracks.values.forEach { it.seek(position) } }
        tracks.values.forEach { it.playback(step.running, mutable.value.speed) }
        val day=Instant.ofEpochMilli(step.positionMillis).atZone(mutable.value.zone).toLocalDate()
        val dayChanged=day!=mutable.value.day
        if(dayChanged&&visibleWindow?.let{step.positionMillis<it.first||step.positionMillis>=it.second}==true)visibleWindow=null
        mutable.update { it.copy(position = step.positionMillis, day=day, wantsPlay = clock.wantsPlayback,
            running = step.running, waiting = step.waiting, phases = tracks.mapValues { row -> row.value.phase() }) }
        if(dayChanged&&zoneKnown)refreshRanges()
    }

    private fun publishIntent() { mutable.update { it.copy(wantsPlay = clock.wantsPlayback) } }
    private fun failure() { mutable.update { it.copy(failures = it.failures + 1) } }

    fun foreground(value: Boolean) {
        if (active == value) return
        active = value; clock.foreground(value)
        tracks.values.forEach { it.foreground(value) }
        if (!value) {
            epoch++; lookups.values.forEach(Job::cancel); lookups.clear()
            prefetches.values.forEach(Job::cancel); prefetches.clear()
            rangesJob?.cancel(); mutable.update { it.copy(running = false, loadingRanges = false) }
        } else { seek(clock.positionMillis); refreshRanges() }
    }

    fun close() { scope.cancel(); tracks.values.forEach(TimelineTrack::close); tracks.clear() }
}
