package ru.kmvms.mobile.core

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.async
import kotlinx.coroutines.test.*
import org.junit.Assert.*
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class PtzDispatcherTest {
    @Test fun repeatedShortTapsExecuteOneByOneAndStopBeforeNext() = runTest {
        val sent = mutableListOf<PtzCommand>()
        val control = PtzDispatcher(backgroundScope, { sent += it; PtzResult(true, stopped = it.action == "stop", continuous = true) }, { testScheduler.currentTime })
        repeat(4) { assertTrue(control.press("move", "left", .08)); control.release() }
        advanceTimeBy(1000); runCurrent()
        assertEquals(listOf("move", "stop", "move", "stop", "move", "stop", "move", "stop"), sent.map { it.action })
        assertEquals(4, sent.filter { it.action == "move" }.map { it.controlId }.toSet().size)
        control.dispose()
    }
    @Test fun shortTapWaitsForCameraAcknowledgementBeforeItsStop() = runTest {
        val acknowledged = CompletableDeferred<Unit>()
        val sent = mutableListOf<PtzCommand>()
        val control = PtzDispatcher(backgroundScope, {
            sent += it; if (it.action == "move") acknowledged.await()
            PtzResult(true, stopped = it.action == "stop", continuous = true)
        }, { testScheduler.currentTime })
        control.press("move", "right", .08); runCurrent(); control.release()
        advanceTimeBy(500); runCurrent()
        assertEquals(listOf("move"), sent.map { it.action })
        acknowledged.complete(Unit); runCurrent(); advanceTimeBy(100); runCurrent()
        assertEquals(listOf("move", "stop"), sent.map { it.action })
        control.dispose()
    }
    @Test fun releasingLongHoldSendsStopEvenWhenInitialReplyIsDelayed() = runTest {
        val reply = CompletableDeferred<Unit>()
        val sent = mutableListOf<PtzCommand>()
        val control = PtzDispatcher(backgroundScope, {
            sent += it; if (it.action == "move") reply.await()
            PtzResult(true, stopped = it.action == "stop", continuous = true)
        }, { testScheduler.currentTime })
        control.press("move", "up", .08); runCurrent(); advanceTimeBy(400); control.release(); runCurrent()
        assertEquals(listOf("move", "stop"), sent.map { it.action })
        reply.complete(Unit); runCurrent(); advanceTimeBy(1500); runCurrent()
        assertEquals(1, sent.count { it.action == "move" })
        control.dispose()
    }
    @Test fun holdRenewsOnlyWhileFingerIsDownAndDisposalStopsIt() = runTest {
        val sent = mutableListOf<PtzCommand>()
        val control = PtzDispatcher(backgroundScope, { sent += it; PtzResult(true, stopped = it.action == "stop", continuous = true, leaseSeconds = 1.5) }, { testScheduler.currentTime })
        control.press("zoom", "in", .04); runCurrent(); advanceTimeBy(1300); runCurrent()
        assertEquals(3, sent.count { it.action == "zoom" })
        control.dispose(); val count = sent.size; advanceTimeBy(2000); runCurrent()
        assertEquals(count, sent.size); assertEquals("stop", sent.last().action)
        assertFalse(control.press("move", "left", .04))
    }
    @Test fun unconfirmedStopClearsQueuedTaps() = runTest {
        val sent = mutableListOf<PtzCommand>(); var errors = 0
        val control = PtzDispatcher(backgroundScope, { sent += it; PtzResult(true, stopped = false) }, { testScheduler.currentTime }, failed = { errors++ })
        repeat(3) { control.press("move", "left", .08); control.release() }
        advanceTimeBy(1000); runCurrent()
        assertEquals(1, sent.count { it.action == "move" }); assertTrue(errors > 0)
        control.dispose()
    }
    @Test fun invalidGeometryNeverReachesTheCamera() = runTest {
        val sent = mutableListOf<PtzCommand>()
        val control = PtzDispatcher(backgroundScope, { sent += it; PtzResult(true) })
        assertFalse(control.point(-.1, .5)); assertFalse(control.point(Double.NaN, .5))
        assertFalse(control.area(.5, .5, .2, .2)); assertFalse(control.area(0.0, 0.0, 1.1, 1.0))
        assertTrue(sent.isEmpty()); control.dispose()
    }
    @Test fun disposingTrackedAreaCancelsItsOwnControlNotAnotherViewer() = runTest {
        val reply = CompletableDeferred<Unit>(); val sent = mutableListOf<PtzCommand>()
        val control = PtzDispatcher(backgroundScope, {
            sent += it; if (it.action == "area_zoom") reply.await()
            PtzResult(true, stopped = it.action == "stop")
        })
        val area = backgroundScope.async { control.area(.1, .2, .6, .8) }; runCurrent()
        control.dispose(); reply.complete(Unit); runCurrent()
        assertFalse(area.await())
        assertEquals(sent.first().controlId, sent.last().controlId)
        assertEquals("stop", sent.last().action)
    }
}
