package ru.kmvms.mobile.events

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import java.time.ZoneOffset

class EventsApiTest {
    private val id = "de5d0fb2-f7f4-4f0b-817a-94a4a567f746"
    private fun client(server: MockWebServer) = EventsApi(NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") })

    @Test fun individualHoursRoundTripWithoutChangingCameraFiltersOrGlobalDefaults() {
        val baseline = EventPreferences(cameraIds = setOf(1, 2), kinds = setOf(CameraEventKind.PERSON),
            hoursEnabled = true, startMinute = 1140, endMinute = 420, perCameraHours = true,
            cameraSchedules = mapOf(1L to EventHours(0, 360), 2L to EventHours(1140, 420)))
        assertEquals(baseline, EventPreferences.from(baseline.json()))
        assertEquals(EventHours(1140, 420), baseline.hoursFor(3))
        assertEquals("per_camera", baseline.json().getString("schedule_mode"))
        assertEquals(EventPreferences(), EventPreferences.from(JSONObject()))
        assertEquals(baseline.cameraSchedules, baseline.copy(perCameraHours = false).cameraSchedules)
    }

    @Test fun receivingUsesOnlyTheExistingSharedCameraSetting() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("{}"))
            client(server).receiving(42, false)
            val request = server.takeRequest()
            assertEquals("PUT", request.method)
            assertEquals("/api/cameras/42", request.path)
            val body = JSONObject(request.body.readUtf8())
            assertEquals(setOf("events_enabled"), body.keys().asSequence().toSet())
            assertFalse(body.getBoolean("events_enabled"))
        }
    }

    @Test fun invalidIndividualHoursCannotBeCreated() {
        for (minutes in listOf(-1, 1440)) try { EventHours(minutes, 0); fail("must reject invalid time") }
        catch (_: IllegalArgumentException) { }
    }

    @Test fun allCamerasAndNoCamerasRemainDifferentPreferences() {
        val all = EventPreferences()
        assertTrue(all.json().isNull("camera_ids"))
        assertTrue(all.json().isNull("kinds"))
        val none = all.copy(cameraIds = emptySet(), kinds = emptySet(), hoursEnabled = true, startMinute = 480, endMinute = 1200)
        assertEquals(0, none.json().getJSONArray("camera_ids").length())
        assertEquals(none, EventPreferences.from(none.json()))
    }

    @Test fun anIntrusionCanCarryBothPersonAndVehicleWithoutLosingItsRule() {
        val event = CameraEvent.from(JSONObject("""{"id":"$id","camera_id":3,"camera_name":"Fixture","kind":"intrusion","occurred_at":"2026-11-14T15:00:00+05:00","object_kinds":["person","vehicle"],"active":true}"""), ZoneOffset.UTC)
        assertEquals(CameraEventKind.INTRUSION, event.kind)
        assertEquals(setOf(CameraEventKind.PERSON, CameraEventKind.VEHICLE), event.objects)
        assertEquals("2026-11-14T10:00:00Z", event.occurred.toString())
    }

    @Test fun historyDeletionUsesTheFrozenSnapshotAndNeverTouchesRecordings() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"deleted":5}"""))
            val through = "2026-11-14T10:00:00.000000|$id"
            client(server).delete(EventDeletion(through = through, count = 5))
            val request = server.takeRequest()
            assertEquals("POST", request.method)
            assertEquals("/api/camera-events/delete-all", request.path)
            assertEquals(through, JSONObject(request.body.readUtf8()).getString("through"))
        }
    }

    @Test fun selectedDeletionAndReadActionsCarryOnlyExplicitEventIds() = runBlocking {
        MockWebServer().use { server ->
            val api = client(server)
            server.enqueue(MockResponse().setBody("""{"deleted":1}"""))
            api.delete(EventDeletion(ids = setOf(id), count = 1))
            val deletion = server.takeRequest()
            assertEquals("/api/camera-events/delete", deletion.path)
            assertEquals(id, JSONObject(deletion.body.readUtf8()).getJSONArray("ids").getString(0))
            server.enqueue(MockResponse().setBody("""{"marked":1}"""))
            api.read(setOf(id))
            assertEquals("/api/camera-events/read", server.takeRequest().path)
        }
    }

    @Test fun ambiguousVideoIdentityIsRetainedAsUnknownInsteadOfGuessingALens() {
        val event = CameraEvent.from(JSONObject("""{"id":"$id","camera_id":null,"camera_name":"Fixture device","kind":"motion","occurred_at":"2026-11-14T10:00:00Z","source_ambiguous":true}"""), ZoneOffset.UTC)
        assertNull(event.cameraId)
        assertTrue(event.ambiguous)
    }

    @Test fun crossPageSelectionFreezesTheOriginalFilterAndUsesOnlyItsReceipt() = runBlocking {
        MockWebServer().use { server ->
            val api = client(server)
            val snapshotId = "b0d1ea55-cb64-42cc-a4ec-68546eca0741"
            val since = "2026-11-07T10:00:00Z"
            val cursor = "2026-11-14T10:00:00.000000|$id"
            val page = EventPage(emptyList(), 75, 0, true, 75, cursor, 75, ZoneOffset.UTC, since)
            val filter = EventFilter(cameraId = 3, kind = CameraEventKind.INTRUSION, unread = true)
            server.enqueue(MockResponse().setBody("""{"id":"$snapshotId","count":75,"visible_ids":["$id"]}"""))
            val selection = api.selectFiltered(filter, page)
            val create = server.takeRequest()
            assertEquals("/api/camera-events/selections", create.path)
            val body = JSONObject(create.body.readUtf8())
            assertEquals(since, body.getString("since")); assertEquals(cursor, body.getString("through"))
            assertEquals(3, body.getInt("camera_id")); assertTrue(body.getBoolean("unread"))
            assertEquals("intrusion", body.getString("kind"))
            val excluded = selection.copy(excluded = setOf(id))
            assertEquals(74, excluded.selectedCount); assertFalse(excluded.contains(id))
            server.enqueue(MockResponse().setBody("""{"items":[],"total":75,"offset":30,"selection":{"id":"$snapshotId","visible_ids":[]}}"""))
            api.page(filter, 30, excluded)
            val request = server.takeRequest().requestUrl!!
            assertEquals(since, request.queryParameter("since")); assertEquals(snapshotId, request.queryParameter("selection_id"))
            server.enqueue(MockResponse().setBody("""{"marked":74}""")); api.readSelection(excluded)
            val read = server.takeRequest()
            assertEquals("/api/camera-events/selections/$snapshotId/read", read.path)
            assertEquals(id, JSONObject(read.body.readUtf8()).getJSONArray("excluded_ids").getString(0))
            server.enqueue(MockResponse().setBody("""{"deleted":74}""")); api.delete(EventDeletion(count=74,snapshot=excluded))
            assertEquals("/api/camera-events/selections/$snapshotId/delete", server.takeRequest().path)
        }
    }

    @Test fun expiredSnapshotCannotFallBackToDeletingTheWholeHistory() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setResponseCode(409).setBody("{}"))
            val selection = EventSelection(id,75,"2026-11-07T10:00:00Z",emptySet())
            try { client(server).delete(EventDeletion(count=75,snapshot=selection)); fail("Must reject expired selection") }
            catch (error: ru.kmvms.mobile.data.NasException) { assertEquals(409,error.httpStatus) }
            assertEquals(1,server.requestCount)
            assertEquals("/api/camera-events/selections/$id/delete",server.takeRequest().path)
        }
    }
}
