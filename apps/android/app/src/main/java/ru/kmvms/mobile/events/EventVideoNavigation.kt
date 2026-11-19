package ru.kmvms.mobile.events

import ru.kmvms.mobile.chronology.ChronologyApi
import ru.kmvms.mobile.chronology.TimelineLookup
import ru.kmvms.mobile.core.ArchiveAvailability
import ru.kmvms.mobile.core.EventAction
import ru.kmvms.mobile.core.EventDestination
import ru.kmvms.mobile.core.eventDestination
import ru.kmvms.mobile.core.notificationEventAction
import ru.kmvms.mobile.data.NasApi
import org.json.JSONObject
import java.time.Duration
import java.time.Instant
import java.time.ZoneId
import java.util.UUID

data class MobileVideoRequest(val cameraId: Long, val cameraName: String, val archiveTime: Long? = null,
                              val key: String = UUID.randomUUID().toString(), val singleCamera: Boolean = false,
                              val returnToEvents: Boolean = false)
data class EventVideoResult(val request: MobileVideoRequest?, val liveFallback: Boolean = false)

/** Navigation uses typed NAS identities, never a server-provided arbitrary URL. */
class EventVideoNavigation(private val api: NasApi) {
    suspend fun open(event: CameraEvent, action: EventAction, zone: ZoneId, now: Instant = Instant.now(),
                     fromPush: Boolean = false): EventVideoResult {
        val id = event.cameraId?.takeIf { it > 0 && !event.ambiguous } ?: return EventVideoResult(null)
        val chosen = if (fromPush) notificationEventAction(action, event.occurred, now) else action
        if (eventDestination(chosen, event.occurred, now, ArchiveAvailability.UNKNOWN) == EventDestination.Live) {
            val result = live(event, id)
            return if (fromPush) result.copy(request = result.request?.copy(singleCamera = true)) else result
        }
        if (event.ai != null) {
            val evidence = EventsApi(api).evidence(event, zone)
            evidence.start?.let { return EventVideoResult(MobileVideoRequest(id, event.camera, (if (fromPush) event.occurred else it).toEpochMilli())) }
            return if (!fromPush && evidence.state == EventArchiveState.PENDING && !event.occurred.isBefore(now.minus(Duration.ofMinutes(2))))
                live(event, id).copy(liveFallback = true) else EventVideoResult(null)
        }
        val chronology = ChronologyApi(api, zone)
        if (fromPush) {
            val timestamp = event.occurred.toEpochMilli()
            return if (chronology.resolve(id, timestamp) is TimelineLookup.Video)
                EventVideoResult(MobileVideoRequest(id, event.camera, timestamp)) else EventVideoResult(null)
        }
        val before = event.occurred.minusSeconds(5).toEpochMilli()
        val preRoll = chronology.resolve(id, before)
        val source = when (preRoll) {
            is TimelineLookup.Video -> preRoll.source
            TimelineLookup.Empty -> (chronology.resolve(id, event.occurred.toEpochMilli()) as? TimelineLookup.Video)?.source
            TimelineLookup.Unavailable -> return EventVideoResult(null)
        }
        if (source != null) return EventVideoResult(MobileVideoRequest(id, event.camera, maxOf(before, source.start)))
        // A fresh event may still belong to an unfinished segment. Be truthful:
        // no claim that recording is guaranteed, and no live fallback for an old
        // missing archive. Explicit LIVE always means the current picture.
        val fresh = !event.occurred.isBefore(now.minus(Duration.ofMinutes(2)))
        return if (fresh) live(event, id).copy(liveFallback = true) else EventVideoResult(null)
    }

    private suspend fun live(event: CameraEvent, cameraId: Long): EventVideoResult {
        val target = api.json("api/camera-events/${event.id}/target?mode=live") as JSONObject
        require(target.getLong("camera_id") == cameraId)
        return EventVideoResult(MobileVideoRequest(cameraId, event.camera, returnToEvents = true))
    }
}
