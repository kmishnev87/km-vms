package ru.kmvms.mobile.core

import java.time.Duration
import java.time.Instant

enum class EventAction { OPEN, LIVE, RECORDING }
enum class ArchiveAvailability { READY, WRITING, MISSING, UNKNOWN }
sealed interface EventDestination {
    data object Live : EventDestination
    data object Recording : EventDestination
    data object LiveWhileRecording : EventDestination
    data object ArchiveUnavailable : EventDestination
    data object CheckArchive : EventDestination
}

/** Explicit Live never becomes old footage. A recording's existence is a NAS fact. */
fun eventDestination(action: EventAction, occurredAt: Instant, now: Instant,
                     archive: ArchiveAvailability, freshness: Duration = Duration.ofMinutes(2)): EventDestination {
    if (action == EventAction.LIVE) return EventDestination.Live
    val recent = !occurredAt.isBefore(now.minus(freshness))
    if (action == EventAction.OPEN && recent) return EventDestination.Live
    return when (archive) {
        ArchiveAvailability.READY -> EventDestination.Recording
        ArchiveAvailability.WRITING -> EventDestination.LiveWhileRecording
        ArchiveAvailability.MISSING -> EventDestination.ArchiveUnavailable
        ArchiveAvailability.UNKNOWN -> EventDestination.CheckArchive
    }
}
