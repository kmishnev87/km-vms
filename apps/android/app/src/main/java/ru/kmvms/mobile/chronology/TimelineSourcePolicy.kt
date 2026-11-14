package ru.kmvms.mobile.chronology

import ru.kmvms.mobile.core.TimelinePhase

/** A stopped empty/failed player retaining an old identity is not prepared media. */
internal fun canReuseTimelineSource(phase: TimelinePhase, covers: Boolean, ended: Boolean): Boolean =
    covers && !ended && phase in setOf(TimelinePhase.READY, TimelinePhase.BUFFERING)
