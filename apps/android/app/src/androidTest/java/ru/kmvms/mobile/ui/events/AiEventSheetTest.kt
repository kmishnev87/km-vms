package ru.kmvms.mobile.ui.events

import android.graphics.Bitmap
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okio.Buffer
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.R
import ru.kmvms.mobile.core.EventAction
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.events.*
import ru.kmvms.mobile.ui.KMVMSTheme
import java.io.ByteArrayOutputStream
import java.time.Instant
import java.time.ZoneOffset
import java.util.concurrent.atomic.AtomicBoolean

@RunWith(AndroidJUnit4::class)
class AiEventSheetTest {
    @get:Rule val compose = createComposeRule()
    @Test fun showsAuthenticatedFrameAndOnlyOffersArchiveWhenAvailable() {
        val id = "de5d0fb2-f7f4-4f0b-817a-94a4a567f746"
        val at = "2026-11-19T09:10:28Z"
        val occurred = Instant.parse(at)
        val record = CameraEvent(id, 7, "Test zone", CameraEventKind.INTRUSION, setOf(CameraEventKind.PERSON),
            occurred, false, false, false, AiEvent(AiEventCondition.EXIT, "", true, false, "zone"))
        val ready = AtomicBoolean(false)
        val authenticated = AtomicBoolean(false)
        val picture = ByteArrayOutputStream().also { Bitmap.createBitmap(16, 9, Bitmap.Config.ARGB_8888)
            .compress(Bitmap.CompressFormat.JPEG, 90, it) }.toByteArray()
        fun event() = """{"id":"$id","camera_id":7,"camera_name":"Test zone","kind":"intrusion","occurred_at":"$at","ai":{"condition":"exit","scope":"zone","snapshot_available":true}}"""
        var action: EventAction? = null
        MockWebServer().use { server ->
            server.dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse = when (request.requestUrl?.encodedPath) {
                    "/api/camera-events/$id/snapshot" -> {
                        authenticated.set(request.getHeader("Authorization") == "Bearer fixture")
                        MockResponse().setHeader("Content-Type","image/jpeg").setBody(Buffer().write(picture))
                    }
                    "/api/camera-events/$id/evidence" -> {
                        val archive = if (ready.get()) """{"state":"ready","camera_id":7,"timestamp_utc":"2026-11-19T09:10:26Z"}"""
                            else """{"state":"pending"}"""
                        MockResponse().setBody("""{"event":${event()},"archive":$archive}""")
                    }
                    else -> MockResponse().setResponseCode(404)
                }
            }
            server.start()
            val api = NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }
            val service = EventsApi(api)
            val context = InstrumentationRegistry.getInstrumentation().targetContext
            compose.setContent { KMVMSTheme {
                AiEventSheet(service, record, ZoneOffset.UTC, canReview = false, canClip = false, opening = false,
                    onDismiss = {}, open = { action = it }, onReview = {}, onClip = { _, _ -> error("no export permission") })
            } }
            fun label(id: Int) = context.getString(id)
            compose.waitUntil(8_000) { compose.onAllNodesWithText(label(R.string.ai_event_pending)).fetchSemanticsNodes().isNotEmpty() }
            compose.waitUntil(8_000) { compose.onAllNodesWithContentDescription(label(R.string.ai_event_frame)).fetchSemanticsNodes().isNotEmpty() }
            compose.onNodeWithText(label(R.string.ai_event_unnamed_zone), substring = true).assertExists()
            compose.onNodeWithText(label(R.string.push_watch_event)).assertDoesNotExist()
            ready.set(true)
            compose.onNodeWithText(label(R.string.ai_event_check)).performScrollTo().performClick()
            compose.waitUntil(8_000) { compose.onAllNodesWithText(label(R.string.push_watch_event)).fetchSemanticsNodes().isNotEmpty() }
            compose.onNodeWithText(label(R.string.push_watch_event)).performScrollTo().performClick()
            compose.runOnIdle { assertEquals(EventAction.RECORDING, action); assertEquals(true, authenticated.get()) }
        }
    }
}
