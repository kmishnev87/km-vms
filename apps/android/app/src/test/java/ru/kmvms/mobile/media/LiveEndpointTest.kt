package ru.kmvms.mobile.media

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.core.Stream
import ru.kmvms.mobile.data.NasApi

class LiveEndpointTest {
    @Test fun ownViewerCanBeReleasedWithoutStoppingOtherUsers() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"viewer_id":"fixture-viewer-1","ok":true}"""))
            server.enqueue(MockResponse().setBody("""{"touched":true}"""))
            server.enqueue(MockResponse().setBody("""{"closed":true}"""))
            val api = NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }
            val live = LiveEndpoint(api)
            val viewer = live.open(12, Stream.SUB)
            assertTrue(live.touch(viewer)); live.close(viewer)
            assertEquals("/api/live/viewers", server.takeRequest().path)
            assertEquals("/api/live/viewers/fixture-viewer-1/touch", server.takeRequest().path)
            val closing = server.takeRequest()
            assertEquals("DELETE", closing.method)
            assertEquals("/api/live/viewers/fixture-viewer-1", closing.path)
        }
    }
    @Test fun renewalUsesStableCameraAndScopedCookiesWithoutTokenInUrl() = runBlocking {
        MockWebServer().use { server ->
            repeat(2) { number -> server.enqueue(MockResponse()
                .setHeader("Set-Cookie", "fixture_media=value-$number; Path=/api/live/12/sub/; HttpOnly")
                .setBody("""{"media_token":"fixture-$number","expires_in":300}""")) }
            val api = NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }
            val live = LiveEndpoint(api)
            val first = live.authorize(12, Stream.SUB)
            val second = live.authorize(12, Stream.SUB)
            assertEquals(255000, first.renewAfterMillis)
            assertNotEquals(first.token, second.token)
            assertFalse(second.toString().contains("fixture-1"))
            repeat(2) { assertEquals("/api/live/media-token", server.takeRequest().path) }
            val scoped = api.mediaClient.cookieJar.loadForRequest(server.url("/api/live/12/sub/index.m3u8"))
            assertEquals("value-1", scoped.single().value)
            assertTrue(api.mediaClient.cookieJar.loadForRequest(server.url("/api/live/13/sub/index.m3u8")).isEmpty())
        }
    }
    @Test fun missingViewerLeaseRequiresReopening() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"touched":false}"""))
            val api = NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }
            assertFalse(LiveEndpoint(api).touch("fixture-expired"))
        }
    }
    @Test fun coldSourceIsAwaitedWithoutReleasingAndRecreatingViewer() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"items":[{"camera_id":12,"stream":"sub","ready":false}]}"""))
            server.enqueue(MockResponse().setBody("""{"items":[{"camera_id":12,"stream":"sub","ready":true}]}"""))
            val api = NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }
            LiveEndpoint(api).awaitReady(12, Stream.SUB, "already-owned-viewer")
            repeat(2) { assertEquals("/api/live/status?camera_id=12&stream=sub", server.takeRequest().path) }
            assertEquals(2, server.requestCount)
        }
    }
    @Test fun shortAuthorityIsRenewedBeforeItExpires() {
        assertEquals(1000, MediaLease("dummy", 10).renewAfterMillis)
        assertEquals(255000, MediaLease("dummy", 300).renewAfterMillis)
    }
}
