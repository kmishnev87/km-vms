package ru.kmvms.mobile.chronology

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import java.time.Instant
import java.time.ZoneOffset
import java.time.ZoneId
import java.time.LocalDateTime
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.RecordedRequest

class ChronologyApiTest {
    private fun client(server: MockWebServer, zone: ZoneId? = ZoneOffset.UTC) = ChronologyApi(NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") },zone)

    @Test fun coldPlaybackAndRangesKnowTheNasZoneBeforeAnyLocalTimestampIsSent() = runBlocking {
        for (zone in listOf("Etc/GMT-5", "America/Los_Angeles", "Asia/Kolkata", "UTC")) MockWebServer().use { server ->
            val target = Instant.parse("2026-11-16T09:05:00Z")
            val expected = target.atZone(ZoneId.of(zone)).toLocalDateTime()
            server.dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    val url = request.requestUrl!!
                    return MockResponse().setBody(when(url.encodedPath) {
                        "/api/system/status" -> """{"initialized":true,"timezone":"$zone"}"""
                        "/api/chronology/ranges" -> {
                            assertEquals(expected,LocalDateTime.parse(url.queryParameter("from")))
                            """{"timezone":{"id":"$zone"},"items":{}}"""
                        }
                        else -> {
                            assertEquals(expected,LocalDateTime.parse(url.queryParameter("ts")))
                            """{"camera_id":3,"segment_id":9,"archive_root_id":"root-a","has_video":true,"offset_sec":5,"duration_sec":60}"""
                        }
                    })
                }
            }
            val api = client(server,null)
            val sources = listOf(async { api.resolve(3,target.toEpochMilli()) },async { api.resolve(3,target.toEpochMilli()) }).awaitAll()
            val source = (sources.first() as TimelineLookup.Video).source
            assertEquals(target.minusSeconds(5).toEpochMilli(),source.start)
            api.ranges(listOf(3),target,target.plusSeconds(60))
            assertEquals(4,server.requestCount)
            assertEquals("/api/system/status",server.takeRequest().path)
        }
    }

    @Test fun invalidOrFailedZoneLookupNeverSendsAnAssumedUtcPlaybackRequest() = runBlocking {
        MockWebServer().use { server ->
            val api = client(server,null)
            for (response in listOf(MockResponse().setResponseCode(503).setBody("{}"),MockResponse().setBody("""{"timezone":"invalid"}"""))) {
                server.enqueue(response)
                assertTrue(runCatching { api.resolve(3,1_000) }.isFailure)
                assertEquals("/api/system/status",server.takeRequest().path)
            }
            server.enqueue(MockResponse().setBody("""{"timezone":"UTC"}"""))
            server.enqueue(MockResponse().setBody("""{"camera_id":3,"has_video":false}"""))
            assertEquals(TimelineLookup.Empty,api.resolve(3,1_000))
            assertEquals(4,server.requestCount)
        }
    }

    @Test fun retainedCameraCatalogIsSeparateFromTheLiveCameraList() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""[{"id":3,"name":"Current","enabled":true},{"id":7,"name":"Old yard","deleted":true,"enabled":false,"host":"","stream_dimensions":{"main":{"width":1920,"height":1080}}}]"""))
            val cameras = client(server).cameras()
            assertEquals("/api/chronology/cameras",server.takeRequest().path)
            assertFalse(cameras[0].deleted); assertTrue(cameras[1].deleted)
            assertEquals("Old yard",cameras[1].name); assertFalse(cameras[1].enabled)
        }
    }

    @Test fun sourceClockUsesResolvedOffsetNotAmbiguousNaiveFileTimestamp() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"camera_id":3,"segment_id":9,"archive_root_id":"root-a","playback_ref":"root-a:9","has_video":true,"offset_sec":10,"duration_sec":60,"file_start":"2026-11-14T15:00:00","mime_type":"video/mp4"}"""))
            val target = Instant.parse("2026-11-14T10:00:10Z").toEpochMilli()
            val source = (client(server).resolve(3, target) as TimelineLookup.Video).source
            assertEquals(Instant.parse("2026-11-14T10:00:00Z").toEpochMilli(), source.start)
            assertTrue(source.covers(target)); assertFalse(source.covers(source.end))
            assertEquals("playback_ref=root-a%3A9", source.query())
        }
    }

    @Test fun unavailableArchiveRootIsNotTreatedAsAnEmptyCamera() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"camera_id":3,"has_video":false,"availability_status":"root_unavailable"}"""))
            assertEquals(TimelineLookup.Unavailable, client(server).resolve(3, 1_000))
            server.enqueue(MockResponse().setBody("""{"camera_id":3,"has_video":false}"""))
            assertEquals(TimelineLookup.Empty, client(server).resolve(3, 1_000))
        }
    }

    @Test fun archiveRangesPreferSystemTimestampsWithTheirActualTimezone() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"timezone":{"id":"Asia/Yekaterinburg"},"items":{"3":{"ranges":[{"start":"2026-11-14T15:00:00","end":"2026-11-14T15:01:00","start_system":"2026-11-14T15:00:00+05:00","end_system":"2026-11-14T15:01:00+05:00"}]}}}"""))
            val result = client(server).ranges(listOf(3, 4), Instant.parse("2026-11-14T00:00:00Z"), Instant.parse("2026-11-15T00:00:00Z"))
            assertEquals(Instant.parse("2026-11-14T10:00:00Z").toEpochMilli(), result.cameras.getValue(3).single().start)
            assertTrue(result.cameras.getValue(4).isEmpty())
        }
    }
    @Test fun requestsUseNasLocalContractSoLegacyArchivesRemainVisible() = runBlocking {
        MockWebServer().use { server ->
            val client = client(server)
            val from = Instant.parse("2026-11-13T19:00:00Z")
            val to = Instant.parse("2026-11-14T19:00:00Z")
            val ranges = """{"timezone":{"id":"Asia/Yekaterinburg"},"items":{}}"""
            server.enqueue(MockResponse().setBody(ranges))
            client.ranges(listOf(3), from, to)
            server.takeRequest()
            server.enqueue(MockResponse().setBody(ranges))
            client.ranges(listOf(3), from, to)
            val rangeRequest = server.takeRequest().requestUrl!!
            assertEquals("2026-11-14T00:00", rangeRequest.queryParameter("from"))
            assertEquals("2026-11-15T00:00", rangeRequest.queryParameter("to"))
            server.enqueue(MockResponse().setBody("""{"camera_id":3,"has_video":false}"""))
            client.resolve(3, Instant.parse("2026-11-14T10:00:10Z").toEpochMilli())
            assertEquals("2026-11-14T15:00:10", server.takeRequest().requestUrl!!.queryParameter("ts"))
        }
    }
}
