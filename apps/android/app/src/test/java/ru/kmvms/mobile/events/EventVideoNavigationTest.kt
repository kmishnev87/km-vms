package ru.kmvms.mobile.events

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.*
import org.json.JSONObject
import org.junit.Test
import ru.kmvms.mobile.core.EventAction
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import java.time.Instant
import java.time.ZoneId
import ru.kmvms.mobile.chronology.ChronologyApi
import ru.kmvms.mobile.chronology.TimelineLookup

class EventVideoNavigationTest {
    private val occurred = Instant.parse("2026-11-14T10:00:00Z")
    private val event = CameraEvent("de5d0fb2-f7f4-4f0b-817a-94a4a567f746", 3, "Fixture", CameraEventKind.MOTION,
        emptySet(), occurred, false, false, false)
    private val zone = ZoneId.of("Asia/Yekaterinburg")
    private fun navigator(server: MockWebServer) = EventVideoNavigation(NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") })

    @Test fun explicitLiveAuthorizesThatCameraEvenWhenTheEventIsOld() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"camera_id":3,"url":"/live?camera_event=fixture"}"""))
            val result = navigator(server).open(event, EventAction.LIVE, zone, occurred.plusSeconds(10_000))
            assertEquals(3L, result.request!!.cameraId)
            assertNull(result.request!!.archiveTime)
            assertTrue(result.request!!.returnToEvents)
            assertTrue(server.takeRequest().path!!.endsWith("/target?mode=live"))
        }
    }

    @Test fun anAvailableRecordingOpensWithFiveSecondPrerollInTheNasTimezone() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"camera_id":3,"has_video":true,"segment_id":9,"archive_root_id":"root","offset_sec":5,"duration_sec":60}"""))
            val result = navigator(server).open(event, EventAction.RECORDING, zone, occurred.plusSeconds(10_000))
            assertEquals(occurred.minusSeconds(5).toEpochMilli(), result.request!!.archiveTime)
            assertFalse(result.request!!.returnToEvents)
            assertEquals("2026-11-14T14:59:55", server.takeRequest().requestUrl!!.queryParameter("ts"))
        }
    }

    @Test fun eventToFreshChronologyPlayerKeepsFourteenOhFiveInsteadOfNineOhFive() = runBlocking {
        MockWebServer().use { server ->
            val api = NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }
            val occurrence = Instant.parse("2026-11-16T09:05:00Z")
            val video = """{"camera_id":3,"has_video":true,"segment_id":9,"archive_root_id":"root","offset_sec":5,"duration_sec":60}"""
            server.enqueue(MockResponse().setBody(video))
            val request = EventVideoNavigation(api).open(event.copy(occurred=occurrence),EventAction.RECORDING,zone,occurrence.plusSeconds(600)).request!!
            assertEquals("2026-11-16T14:04:55",server.takeRequest().requestUrl!!.queryParameter("ts"))
            server.enqueue(MockResponse().setBody("""{"initialized":true,"timezone":"Etc/GMT-5"}"""))
            server.enqueue(MockResponse().setBody(video))
            val source=(ChronologyApi(api).resolve(request.cameraId,request.archiveTime!!) as TimelineLookup.Video).source
            assertEquals("/api/system/status",server.takeRequest().path)
            assertEquals("2026-11-16T14:04:55",server.takeRequest().requestUrl!!.queryParameter("ts"))
            assertEquals(occurrence.minusSeconds(10).toEpochMilli(),source.start)
            assertEquals(5_000L,request.archiveTime!!-source.start)
        }
    }

    @Test fun missingOldArchiveDoesNotSilentlyShowCurrentVideo() = runBlocking {
        MockWebServer().use { server ->
            repeat(2) { server.enqueue(MockResponse().setBody("""{"has_video":false}""")) }
            val result = navigator(server).open(event, EventAction.RECORDING, zone, occurred.plusSeconds(10_000))
            assertNull(result.request)
            assertEquals(2, server.requestCount)
        }
    }

    @Test fun aiRequiresExactArchiveProofAndUsesItsUtcOffset() = runBlocking {
        MockWebServer().use { server ->
            val ai = JSONObject().put("condition", "exit").put("rule_name", "Gate")
            val row = JSONObject().put("id", event.id).put("camera_id", 3).put("camera_name", "Fixture")
                .put("kind", "intrusion").put("occurred_at", occurred.toString()).put("ai", ai)
            val body = JSONObject().put("event", row)
            val aiEvent = CameraEvent.from(row, zone)
            server.enqueue(MockResponse().setBody(body.put("archive", JSONObject().put("state", "unavailable")).toString()))
            assertNull(navigator(server).open(aiEvent, EventAction.RECORDING, zone, occurred.plusSeconds(240)).request)
            assertEquals("/api/camera-events/${event.id}/evidence", server.takeRequest().path)
            val start = occurred.minusSeconds(2)
            server.enqueue(MockResponse().setBody(body.put("archive", JSONObject().put("state", "ready")
                .put("camera_id", 3).put("timestamp_utc", start.toString())).toString()))
            assertEquals(start.toEpochMilli(),
                navigator(server).open(aiEvent, EventAction.RECORDING, zone, occurred.plusSeconds(240)).request?.archiveTime)
            assertEquals(2, server.requestCount)
        }
    }

    @Test fun freshMissingArchiveFallsBackToLiveWithAnExplicitNotice() = runBlocking {
        MockWebServer().use { server ->
            repeat(2) { server.enqueue(MockResponse().setBody("""{"has_video":false}""")) }
            server.enqueue(MockResponse().setBody("""{"camera_id":3}"""))
            val result = navigator(server).open(event, EventAction.RECORDING, zone, occurred.plusSeconds(10))
            assertNotNull(result.request)
            assertTrue(result.liveFallback)
            assertNull(result.request!!.archiveTime)
            assertTrue(result.request!!.returnToEvents)
            assertEquals(3, server.requestCount)
        }
    }
}
