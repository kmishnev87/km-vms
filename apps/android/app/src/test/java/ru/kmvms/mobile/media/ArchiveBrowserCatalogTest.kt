package ru.kmvms.mobile.media

import kotlinx.coroutines.*
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.test.resetMain
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.archive.*
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import java.util.concurrent.TimeUnit

@OptIn(ExperimentalCoroutinesApi::class)
class ArchiveBrowserCatalogTest {
    private val catalog = """{"items":["Yard"],"export_items":[],"camera_items":[{"id":"9","name":"Yard","deleted":true,"value":"camera:9"}]}"""
    private val page = """{"items":[],"total":1,"pagination":{"has_more":false},"timezone":{"id":"UTC"}}"""
    private fun browser(server: MockWebServer) = ArchiveBrowser(ArchiveApi(NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }))

    @Test fun restoredCameraWaitsForDelayedCatalogInsteadOfSearchingItsTokenAsAName() = runBlocking {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try { MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody(catalog).setBodyDelay(400, TimeUnit.MILLISECONDS))
            server.enqueue(MockResponse().setBody(page))
            val browser = browser(server)
            try {
                browser.search("camera:9", null)
                assertEquals("/api/recordings/cameras", server.takeRequest(2, TimeUnit.SECONDS)!!.path)
                assertNull("No file query until catalog identity is known", server.takeRequest(100, TimeUnit.MILLISECONDS))
                withTimeout(5000) { browser.state.first { !it.loading && it.total == 1 } }
                val request = server.takeRequest(2, TimeUnit.SECONDS)!!.requestUrl!!
                assertEquals("9", request.queryParameter("camera_id"))
                assertNull(request.queryParameter("camera"))
                assertTrue(browser.state.value.cameras.single().deleted)
            } finally { browser.close() }
        } } finally { Dispatchers.resetMain() }
    }

    @Test fun refreshRetriesAFailedCatalogAndKeepsTheSavedCameraIdentity() = runBlocking {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try { MockWebServer().use { server ->
            server.enqueue(MockResponse().setResponseCode(503).setBody("{}"))
            val browser = browser(server)
            try {
                withTimeout(5000) { browser.state.first { it.failed } }
                assertEquals("/api/recordings/cameras", server.takeRequest(2, TimeUnit.SECONDS)!!.path)
                server.enqueue(MockResponse().setBody(catalog)); server.enqueue(MockResponse().setBody(page))
                browser.search("camera:9", null)
                withTimeout(5000) { browser.state.first { !it.loading && !it.failed && it.total == 1 } }
                assertEquals("/api/recordings/cameras", server.takeRequest(2, TimeUnit.SECONDS)!!.path)
                assertEquals("9", server.takeRequest(2, TimeUnit.SECONDS)!!.requestUrl!!.queryParameter("camera_id"))
            } finally { browser.close() }
        } } finally { Dispatchers.resetMain() }
    }
}
