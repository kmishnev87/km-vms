package ru.kmvms.mobile.core

import org.junit.Assert.*
import org.junit.Test

class LiveClockPolicyTest {
    @Test fun nearLiveDoesNotChangeSpeedOrPitch() {
        assertEquals(1f, lowLatencyPlaybackRate(200, 0))
        assertEquals(1f, lowLatencyPlaybackRate(-100, 0))
    }
    @Test fun queuedBytesAndDemuxedSamplesBothContributeToCatchup() {
        assertTrue(lowLatencyPlaybackRate(100, 800) > 1.05f)
        assertEquals(1.25f, lowLatencyPlaybackRate(1600, 0))
    }
    @Test fun cameraClockDriftDoesNotGrowIntoSecondsOfPtzDelay() {
        var ahead = 200.0
        var largest = ahead
        repeat(3600) {
            // Six minutes, 100ms steps, producer's timestamps run 1% faster.
            ahead += 101 - 100 * lowLatencyPlaybackRate(ahead.toLong(), 0)
            largest = maxOf(largest, ahead)
        }
        assertTrue("clock drift must remain bounded", largest < 500)
        assertTrue(ahead < 500)
    }
}
