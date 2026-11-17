package ru.kmvms.mobile.chronology

import kotlinx.coroutines.*
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.ExperimentalCoroutinesApi
import org.junit.Assert.*
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class ChronologyReadQueueTest {
    @Test fun fiveSelectedCamerasPrepareTogetherRatherThanInBatchesOfThree() = runTest {
        val reads = ChronologyReadQueue(5)
        val entered = mutableListOf<Int>()
        val release = CompletableDeferred<Unit>()
        val jobs = (1..6).map { id -> async { reads.foreground { entered += id; release.await() } } }
        runCurrent()
        assertEquals((1..5).toList(), entered)
        release.complete(Unit)
        jobs.awaitAll()
        assertEquals((1..6).toList(), entered)
    }

    @Test fun interactiveReadsOvertakeQueuedPrefetchAndOnlyOnePrefetchIsActive() = runTest {
        val reads = ChronologyReadQueue(2)
        val entered = mutableListOf<String>()
        val releaseFirst = CompletableDeferred<Unit>(); val releaseCurrent = CompletableDeferred<Unit>()
        val releaseNext = CompletableDeferred<Unit>()
        val first = async { reads.prefetch { entered += "prefetch-1"; releaseFirst.await() } }
        val queued = async { reads.prefetch { entered += "prefetch-2" } }
        runCurrent()
        assertEquals(listOf("prefetch-1"), entered)
        val current = async { reads.foreground { entered += "current-1"; releaseCurrent.await() } }
        val next = async { reads.foreground { entered += "current-2"; releaseNext.await() } }
        runCurrent()
        assertEquals(listOf("prefetch-1", "current-1"), entered)
        releaseFirst.complete(Unit); runCurrent()
        assertEquals(listOf("prefetch-1", "current-1", "current-2"), entered)
        releaseNext.complete(Unit); runCurrent()
        assertEquals("prefetch-2", entered.last())
        releaseCurrent.complete(Unit)
        listOf(first, queued, current, next).awaitAll()
    }

    @Test fun cancellingAQueuedSeekDoesNotConsumeAReadSlotLater() = runTest {
        val reads = ChronologyReadQueue(1)
        val release = CompletableDeferred<Unit>()
        val first = async { reads.foreground { release.await() } }
        val obsolete = async { reads.foreground { fail("obsolete position was queried") } }
        runCurrent(); obsolete.cancelAndJoin()
        val next = async { reads.foreground { "new position" } }
        release.complete(Unit); first.await()
        assertEquals("new position", next.await())
    }

    @Test fun cancellingActiveReadsReleasesAllCapacityForANewSeek() = runTest {
        val reads = ChronologyReadQueue(5)
        val old = (1..5).map { async { reads.foreground { awaitCancellation() } } }
        runCurrent()
        old.forEach { it.cancelAndJoin() }
        val current = (1..5).map { id -> async { reads.foreground { id } } }.awaitAll()
        assertEquals((1..5).toList(), current)
    }

    @Test fun aFailedPrefetchDoesNotBlockTheCurrentMomentOrFuturePrefetch() = runTest {
        val reads = ChronologyReadQueue(1)
        assertTrue(runCatching { reads.prefetch { error("fixture unavailable") } }.isFailure)
        assertEquals("current", reads.foreground { "current" })
        assertEquals("next", reads.prefetch { "next" })
    }

    @Test fun foregroundCancellationWhileAdmissionIsBeingDeliveredCannotLeakCapacity() = runTest {
        val reads = ChronologyReadQueue(1)
        val first = CompletableDeferred<Unit>()
        val active = async { reads.foreground { first.await() } }
        val pending = async { reads.foreground { fail("cancelled work must not run") } }
        runCurrent()
        first.complete(Unit)
        pending.cancel()
        active.await(); pending.join()
        assertEquals(7, reads.foreground { 7 })
    }

    @Test fun invalidCapacityIsRejected() {
        assertTrue(runCatching { ChronologyReadQueue(0) }.isFailure)
    }
}
