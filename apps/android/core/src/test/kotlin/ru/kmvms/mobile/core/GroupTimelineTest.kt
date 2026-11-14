package ru.kmvms.mobile.core

import org.junit.Assert.*
import org.junit.Test

class GroupTimelineTest {
    private fun ready(id: Long, at: Long) = TimelineSample(id, TimelinePhase.READY, at)
    private fun timeline() = GroupTimeline(10_000).apply { prepare(10_000, setOf(1, 2, 3, 4), true) }

    @Test fun playBeforeLoadingWaitsForEveryRealRecordingButNotAnEmptyCamera() {
        val clock = timeline()
        val loading = listOf(ready(1, 10_000), ready(2, 10_000), TimelineSample(3, TimelinePhase.BUFFERING), TimelineSample(4, TimelinePhase.EMPTY))
        val waiting = clock.step(loading, 100, 1f)
        assertFalse(waiting.running)
        assertEquals(setOf(3L), waiting.waiting)
        assertEquals(10_000, waiting.positionMillis)
        assertTrue(clock.step(loading.map { if (it.cameraId == 3L) ready(3, 10_000) else it }, 100, 1f).running)
    }

    @Test fun stalledFourthCameraFreezesTheEntireClockUntilItCatchesUp() {
        val clock = timeline()
        assertTrue(clock.step((1L..4L).map { ready(it, 10_000) }, 100, 1f).running)
        val stalled = listOf(ready(1, 11_000), ready(2, 11_000), ready(3, 11_000), TimelineSample(4, TimelinePhase.BUFFERING, 10_800))
        val hold = clock.step(stalled, 100, 1f)
        assertFalse(hold.running)
        assertEquals(10_800, hold.positionMillis)
        assertEquals(10_800L, hold.seekToMillis)
        repeat(100) { assertEquals(10_800, clock.step(stalled, 100, 1f).positionMillis) }
        assertTrue(clock.step((1L..4L).map { ready(it, 10_800) }, 100, 1f).running)
    }

    @Test fun errorsAndUnresolvedCamerasNeverMasqueradeAsEmpty() {
        val clock = timeline()
        val result = clock.step(listOf(ready(1, 10_000), TimelineSample(2, TimelinePhase.EMPTY), TimelineSample(3, TimelinePhase.FAILED)), 100, 1f)
        assertFalse(result.running)
        assertEquals(setOf(3L, 4L), result.waiting)
    }

    @Test fun driftPausesAndResynchronizesInsteadOfRunningDifferentMoments() {
        val clock = timeline()
        clock.step((1L..4L).map { ready(it, 10_000) }, 100, 1f)
        val result = clock.step(listOf(ready(1, 12_000), ready(2, 11_000), ready(3, 11_000), ready(4, 11_000)), 100, 1f)
        assertFalse(result.running)
        assertEquals(11_000L, result.seekToMillis)
    }

    @Test fun aCommonEmptyIntervalCanAdvanceWithoutInventingVideo() {
        val clock = timeline()
        val result = clock.step((1L..4L).map { TimelineSample(it, TimelinePhase.EMPTY) }, 100, 2f)
        assertTrue(result.running)
        assertEquals(10_200, result.positionMillis)
    }

    @Test fun removingAFailedCameraReleasesTheRemainingGroup() {
        val clock = timeline()
        clock.prepare(10_000, setOf(1, 2), true)
        assertTrue(clock.step(listOf(ready(1, 10_000), ready(2, 10_000)), 100, 1f).running)
    }

    @Test fun backgroundStopsPlaybackWithoutLosingTheExplicitPlayIntent() {
        val clock = timeline()
        clock.foreground(false)
        assertFalse(clock.step((1L..4L).map { ready(it, 10_000) }, 100, 1f).running)
        assertTrue(clock.wantsPlayback)
        clock.foreground(true)
        assertTrue(clock.step((1L..4L).map { ready(it, 10_000) }, 100, 1f).running)
        clock.play(false)
        assertFalse(clock.step((1L..4L).map { ready(it, 10_000) }, 100, 1f).running)
    }
}
