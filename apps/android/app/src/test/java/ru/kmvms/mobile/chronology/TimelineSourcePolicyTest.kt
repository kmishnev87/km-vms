package ru.kmvms.mobile.chronology

import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.core.TimelinePhase

class TimelineSourcePolicyTest {
    @Test fun returningFromAnEmptyIntervalMustPrepareTheRetainedRecordingAgain() {
        assertFalse(canReuseTimelineSource(TimelinePhase.EMPTY, covers = true, ended = false))
        assertFalse(canReuseTimelineSource(TimelinePhase.RESOLVING, covers = true, ended = false))
        assertFalse(canReuseTimelineSource(TimelinePhase.FAILED, covers = true, ended = false))
    }

    @Test fun anEndedSourceOrAnotherIntervalCannotBeReusedAsReadyMedia() {
        assertFalse(canReuseTimelineSource(TimelinePhase.BUFFERING, covers = true, ended = true))
        assertFalse(canReuseTimelineSource(TimelinePhase.READY, covers = false, ended = false))
        assertTrue(canReuseTimelineSource(TimelinePhase.READY, covers = true, ended = false))
    }
}
