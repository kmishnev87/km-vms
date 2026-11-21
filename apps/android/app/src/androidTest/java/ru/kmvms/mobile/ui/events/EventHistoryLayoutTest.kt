package ru.kmvms.mobile.ui.events

import android.content.res.Configuration
import android.graphics.Bitmap
import androidx.compose.foundation.layout.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.events.*
import ru.kmvms.mobile.ui.KMVMSTheme
import java.time.Instant
import java.time.ZoneOffset
import java.io.File
import java.util.Locale
import java.util.concurrent.CopyOnWriteArrayList

@RunWith(AndroidJUnit4::class)
class EventHistoryLayoutTest {
    @get:Rule val compose = createComposeRule()
    private val event = CameraEvent("de5d0fb2-f7f4-4f0b-817a-94a4a567f746", 1, "Panorama",
        CameraEventKind.INTRUSION, emptySet(), Instant.parse("2026-11-17T10:00:00Z"), false, false, false)
    private val page = EventPage(listOf(event), 165, 0, true, 2, "fixture-cursor", 165, ZoneOffset.UTC)

    private fun compactFilters(locale: String) {
        var filter by mutableStateOf(EventFilter(unread=true))
        compose.setContent {
            val config = Configuration(LocalConfiguration.current).apply { setLocale(Locale.forLanguageTag(locale)) }
            val context = LocalContext.current.createConfigurationContext(config)
            CompositionLocalProvider(LocalContext provides context, LocalConfiguration provides config) {
                KMVMSTheme { Column(Modifier.width(320.dp)) { EventFilters(EventsState(filter=filter)) { filter=it } } }
            }
        }
        val camera = compose.onNodeWithTag("events-filter-camera").getUnclippedBoundsInRoot()
        val kind = compose.onNodeWithTag("events-filter-kind").getUnclippedBoundsInRoot()
        val days = compose.onNodeWithTag("events-filter-days").getUnclippedBoundsInRoot()
        for (bounds in listOf(camera, kind, days)) assertEquals(36f, (bounds.bottom-bounds.top).value, .5f)
        assertEquals(camera.top, kind.top)
        assertEquals(camera.left, days.left)
        assertEquals(kind.right, days.right)
        assertTrue(days.top-camera.bottom >= 12.dp)
        compose.onNodeWithTag("events-unread-only").assertDoesNotExist()
        compose.onNodeWithTag("events-filter-days").performClick()
        compose.onNodeWithText("30", substring=true).performClick()
        compose.runOnIdle { assertEquals(30,filter.days); assertTrue(filter.unread) }
    }
    @Test fun compactRussianFilters() = compactFilters("ru")
    @Test fun compactEnglishFilters() = compactFilters("en")
    @Test fun compactChineseFilters() = compactFilters("zh-CN")

    @Test fun namesKindsAndTimesStartOnTheSameLineForReadAndUnreadEvents() {
        compose.setContent { KMVMSTheme { Column(Modifier.width(360.dp)) {
            EventRow(event, EventsState(page=page), true, false, {}, {})
            EventRow(event.copy(id="read",read=true), EventsState(page=page), true, false, {}, {})
        } } }
        for (id in listOf(event.id,"read")) {
            val camera=compose.onNodeWithTag("event-camera-$id",useUnmergedTree=true).getUnclippedBoundsInRoot()
            val kind=compose.onNodeWithTag("event-kind-$id",useUnmergedTree=true).getUnclippedBoundsInRoot()
            val time=compose.onNodeWithTag("event-time-$id",useUnmergedTree=true).getUnclippedBoundsInRoot()
            assertEquals(camera.left,kind.left); assertEquals(kind.left,time.left)
        }
        assertEquals(compose.onNodeWithTag("event-camera-${event.id}",useUnmergedTree=true).getUnclippedBoundsInRoot().left,
            compose.onNodeWithTag("event-camera-read",useUnmergedTree=true).getUnclippedBoundsInRoot().left)
    }

    @Test fun bothSourcesUseTheSamePhotoActionOutsideEventText() {
        val other = event.copy(id = "ai-fixture", camera = "PTZ", ai = AiEvent(AiEventCondition.SCENE, "PTZ", true, false))
        var opened = ""
        compose.setContent {
            val config = Configuration(LocalConfiguration.current).apply { setLocale(Locale.forLanguageTag("ru")) }
            val local = LocalContext.current.createConfigurationContext(config)
            CompositionLocalProvider(LocalContext provides local, LocalConfiguration provides config) {
                KMVMSTheme { Column(Modifier.width(340.dp).testTag("combined-event-photo-preview")) {
                    listOf(event, other).forEach { row ->
                        EventRow(row, EventsState(page = page), true, false, {}, {}, { opened = row.id })
                    }
                } }
            }
        }
        val columns = listOf(event, other).map { row ->
            val endOfText = compose.onNodeWithTag("event-kind-${row.id}", useUnmergedTree = true).getUnclippedBoundsInRoot().right
            val photo = compose.onNodeWithTag("event-photo-${row.id}")
            val actions = listOf(photo.getUnclippedBoundsInRoot(), compose.onNodeWithTag("event-live-${row.id}").getUnclippedBoundsInRoot(),
                compose.onNodeWithTag("event-archive-${row.id}").getUnclippedBoundsInRoot())
            assertTrue(actions[0].left >= endOfText)
            assertTrue(actions[0].right <= actions[1].left && actions[1].right <= actions[2].left)
            photo.performClick()
            compose.runOnIdle { assertEquals(row.id, opened) }
            actions.map { it.left }
        }
        assertEquals(columns[0], columns[1])
        if (InstrumentationRegistry.getArguments().getString("kmvmsCapture") == "true") {
            val image = compose.onNodeWithTag("combined-event-photo-preview").captureToImage().asAndroidBitmap()
            File(InstrumentationRegistry.getInstrumentation().targetContext.cacheDir, "six-event-photo-row.png")
                .outputStream().use { image.compress(Bitmap.CompressFormat.PNG, 100, it) }
        }
    }

    @Test fun foundAndSelectAllStayVerticallyCenteredInOneFixedHeightRow() {
        var selected by mutableStateOf(false)
        var clicked=false
        compose.setContent { KMVMSTheme {
            EventSelectionSummary(EventsState(page=page,selected=if(selected)setOf(event.id) else emptySet()),
                selected, Modifier.width(220.dp).height(48.dp)) { clicked=true }
        } }
        val initial=compose.onNodeWithTag("events-selection-count").getUnclippedBoundsInRoot()
        assertEquals(24f,(initial.top+(initial.bottom-initial.top)/2).value,.5f)
        compose.onNodeWithTag("events-select-filtered").assertDoesNotExist()
        compose.runOnIdle { selected=true }
        val count=compose.onNodeWithTag("events-selection-count").getUnclippedBoundsInRoot()
        val select=compose.onNodeWithTag("events-select-filtered").getUnclippedBoundsInRoot()
        assertEquals(24f,(count.top+(count.bottom-count.top)/2).value,.5f)
        assertEquals(24f,(select.top+(select.bottom-select.top)/2).value,.5f)
        assertTrue(select.left>count.right)
        compose.onNodeWithTag("events-select-filtered").performClick()
        compose.runOnIdle { assertTrue(clicked) }
    }

    @Test fun disabledUnreadFilterCannotChangeTheChoice() {
        compose.setContent { KMVMSTheme { EventUnreadFilter(false, false) { fail("busy filter must not change") } } }
        compose.onNodeWithTag("events-unread-only").assertIsOff().assertIsNotEnabled()
    }

    @Test fun toolbarToggleFindsOlderUnreadEventsWithoutReadActionsOrLosingOtherFilters() {
        val requests=CopyOnWriteArrayList<RecordedRequest>()
        val server=MockWebServer()
        server.dispatcher=object:Dispatcher() {
            override fun dispatch(request:RecordedRequest):MockResponse {
                requests+=request
                val path=request.requestUrl!!.encodedPath
                val body=when(path) {
                    "/api/camera-events" -> {
                        val unread=request.requestUrl!!.queryParameter("unread")=="true"
                        val name=if(unread)"Older unviewed camera" else "Already viewed camera"
                        """{"items":[{"id":"${event.id}","camera_id":1,"camera_name":"$name","kind":"person","occurred_at":"2026-11-17T10:00:00Z","read":${!unread}}],"total":${if(unread)2 else 165},"unread_count":2,"timezone":"UTC","history_count":165}"""
                    }
                    "/api/camera-events/cameras" -> """{"items":[]}"""
                    "/api/camera-events/preferences" -> "{}"
                    "/api/camera-events/updates" -> """{"changed":false,"items":[]}"""
                    else -> return MockResponse().setResponseCode(404)
                }
                return MockResponse().setBody(body)
        }
    }

        server.start()
        try {
            val api=NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }
            compose.setContent { KMVMSTheme { EventsScreen(api,false,true,{}, { error(it) }) } }
            compose.waitUntil(5_000) { compose.onAllNodesWithText("Already viewed camera").fetchSemanticsNodes().isNotEmpty() }
            val filter=compose.onNodeWithTag("events-open-filters").getUnclippedBoundsInRoot()
            val unread=compose.onNodeWithTag("events-unread-only").getUnclippedBoundsInRoot()
            val alerts=compose.onNodeWithTag("events-alerts-tab").getUnclippedBoundsInRoot()
            assertTrue(unread.left>=filter.right && unread.right<=alerts.left)
            compose.onNodeWithTag("events-unread-only").assertIsOff().performClick().assertIsOn()
            compose.waitUntil(5_000) { compose.onAllNodesWithText("Older unviewed camera").fetchSemanticsNodes().isNotEmpty() }
            compose.onNodeWithText("Already viewed camera").assertDoesNotExist()
            compose.onNodeWithTag("events-unread-only").performClick().assertIsOff()
            compose.waitUntil(5_000) { compose.onAllNodesWithText("Already viewed camera").fetchSemanticsNodes().isNotEmpty() }
            val filtered=requests.filter { it.requestUrl!!.queryParameter("unread")=="true" }
            assertTrue(filtered.isNotEmpty())
            assertTrue(filtered.all { it.requestUrl!!.queryParameter("offset")=="0" && it.method=="GET" })
            assertTrue(requests.all { it.method=="GET" })
        } finally { server.shutdown() }
    }

    @Test fun openingThePhotoMarksOnlyThatEventAndSelectionAloneDoesNotMarkIt() = MockWebServer().use { server ->
        val another = "1fdbc11a-2e3f-43a0-a1ac-44aaab711111"
        val reads = CopyOnWriteArrayList<String>()
        fun row(id: String, name: String) =
            """{"id":"$id","camera_id":1,"camera_name":"$name","kind":"person","occurred_at":"2026-11-17T10:00:00Z","read":${id in reads},"snapshot_available":false}"""
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val body = when (request.requestUrl!!.encodedPath) {
                    "/api/camera-events" -> """
                        {"items":[${row(event.id, "First camera")},${row(another, "Second camera")}],"total":2,
                         "unread_count":${2 - reads.size},"timezone":"UTC","history_count":2}
                    """.trimIndent()
                    "/api/camera-events/cameras" -> """{"items":[]}"""
                    "/api/camera-events/preferences" -> "{}"
                    "/api/camera-events/updates" -> """{"changed":false,"items":[]}"""
                    "/api/camera-events/${event.id}/evidence" ->
                        """{"event":${row(event.id, "First camera")},"archive":{"state":"unavailable"}}"""
                    "/api/camera-events/read" -> {
                        val ids = org.json.JSONObject(request.body.readUtf8()).getJSONArray("ids")
                        for (i in 0 until ids.length()) reads += ids.getString(i)
                        """{"marked":${ids.length()}}"""
                    }
                    else -> return MockResponse().setResponseCode(404)
                }
                return MockResponse().setBody(body)
            }
        }
        server.start()
        val api = NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }
        compose.setContent { KMVMSTheme { EventsScreen(api, false, true, {}, { error(it) }) } }
        compose.waitUntil(5_000) { compose.onAllNodesWithText("First camera").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("event-check-$another").performClick().assertIsOn()
        compose.runOnIdle { assertTrue(reads.isEmpty()) }
        compose.onNodeWithTag("event-photo-${event.id}").performClick()
        compose.waitUntil(5_000) { reads.isNotEmpty() }
        compose.onNodeWithTag("camera-event-detail").assertIsDisplayed()
        assertEquals(listOf(event.id), reads.toList())
    }
}
