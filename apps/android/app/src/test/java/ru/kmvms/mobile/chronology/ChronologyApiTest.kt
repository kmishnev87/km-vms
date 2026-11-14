package ru.kmvms.mobile.chronology

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import java.time.Instant

class ChronologyApiTest {
    private fun client(server: MockWebServer) = ChronologyApi(NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") })

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
