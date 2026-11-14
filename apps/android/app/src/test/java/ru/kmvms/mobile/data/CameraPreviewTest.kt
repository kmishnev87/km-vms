package ru.kmvms.mobile.data

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.MockResponse
import okio.Buffer
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.core.ServerAddress

class CameraPreviewTest {
    @Test fun savedPreviewUsesOnlyTheSelectedNasAuthenticatedPath()=runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setHeader("Content-Type","image/jpeg").setBody(Buffer().write(byteArrayOf(1,2,3))))
            val api=NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }
            assertArrayEquals(byteArrayOf(1,2,3),api.cameraPreview(7))
            val request=server.takeRequest()
            assertEquals("/api/previews/camera-previews/7.jpg",request.path)
            assertEquals("Bearer fixture",request.getHeader("Authorization"))
        }
    }
    @Test fun previewRedirectNeverForwardsNasCredentials()=runBlocking {
        MockWebServer().use { server ->MockWebServer().use { other ->
            server.enqueue(MockResponse().setResponseCode(302).setHeader("Location",other.url("/unexpected")))
            val api=NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }
            assertTrue(runCatching { api.cameraPreview(7) }.isFailure)
            assertEquals(0,other.requestCount)
        } }
    }
}
