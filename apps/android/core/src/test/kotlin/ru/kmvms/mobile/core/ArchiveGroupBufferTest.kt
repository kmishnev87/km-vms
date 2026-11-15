package ru.kmvms.mobile.core

import org.junit.Assert.*
import org.junit.Test

class ArchiveGroupBufferTest {
    private fun ready(id:Long,buffer:Long,remaining:Long=100_000,capacity:Long=Long.MAX_VALUE)=
        TimelineSample(id,TimelinePhase.READY,10_000,buffer,remaining,capacity)
    private fun clock()=GroupTimeline(10_000).apply{prepare(10_000,setOf(1,2,3,4),true)}
    private fun full()=(1L..4L).map{ready(it,60_000)}
    private fun starve(clock:GroupTimeline){
        val samples=full().map{if(it.cameraId==1L)it.copy(phase=TimelinePhase.BUFFERING,bufferedAheadMillis=0) else it}
        assertFalse(clock.step(samples,100,1f).running)
    }
    @Test fun normalFastStartupNeedsNoRecoveryReserveOrFixedWait(){
        val clock=clock()
        assertTrue(clock.step((1L..4L).map{ready(it,600)},1,1f).running)
        assertEquals(500,clock.bufferReserveMillis)
    }
    @Test fun recoveryWaitsForAllCamerasNotOnlyTheOneThatOriginallyStalled(){
        val clock=clock();clock.step(full(),100,1f);starve(clock)
        assertEquals(8000,clock.bufferReserveMillis)
        val small=listOf(ready(1,9000),ready(2,2000),ready(3,6000),ready(4,1000))
        val held=clock.step(small,100,1f)
        assertEquals(setOf(2L,3L,4L),held.waiting);assertEquals(10_000,held.positionMillis)
        assertFalse(held.running)
        assertTrue(clock.step(full(),100,1f).running)
    }
    @Test fun repeatedStarvationRaisesReserveButNeverBeyondTheBoundedMaximum(){
        val clock=clock();clock.step(full(),100,1f)
        listOf(8000L,12000L,18000L,18000L).forEach{target->
            starve(clock);assertEquals(target,clock.bufferReserveMillis)
            assertFalse(clock.step((1L..4L).map{ready(it,target-100)},100,1f).running)
            assertTrue(clock.step(full(),100,1f).running)
        }
    }
    @Test fun shortFilesGapsSpeedAndMemoryLimitsCannotDeadlockTheBarrier(){
        val clock=clock();clock.step(full(),100,1f);starve(clock)
        val source=listOf(ready(1,2000,remaining=2000),TimelineSample(2,TimelinePhase.EMPTY),
            ready(3,3000,capacity=2900),ready(4,32_000))
        assertTrue(clock.step(source,100,4f).running)
    }
    @Test fun realErrorsStayBlockingAndManualPauseRejectsLateReadyBuffers(){
        val clock=clock();clock.step(full(),100,1f);starve(clock)
        assertFalse(clock.step(full().map{if(it.cameraId==3L)it.copy(phase=TimelinePhase.FAILED) else it},100,1f).running)
        clock.play(false);assertFalse(clock.step(full(),100,1f).running)
        clock.prepare(10_000,setOf(1,2,3,4),true)
        assertEquals(500,clock.bufferReserveMillis)
        assertTrue(clock.step(full(),100,1f).running)
    }
    @Test fun reserveDecaysOnlyAfterSustainedPlaybackNotWhileWaiting(){
        val policy=ArchiveBufferPolicy();repeat(3){policy.starved()}
        repeat(119){policy.progressed(500)};assertEquals(18000,policy.reserveMillis)
        policy.progressed(500);assertEquals(12000,policy.reserveMillis)
        repeat(120){policy.progressed(500)};assertEquals(8000,policy.reserveMillis)
    }
}
