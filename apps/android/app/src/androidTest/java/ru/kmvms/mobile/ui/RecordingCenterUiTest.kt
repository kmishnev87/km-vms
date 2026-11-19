package ru.kmvms.mobile.ui

import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.R
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.ui.recording.RecordingCenterScreen
import java.util.concurrent.atomic.AtomicReference

@RunWith(AndroidJUnit4::class)
class RecordingCenterUiTest {
    @get:Rule val compose = createComposeRule()

    private fun str(id: Int) = InstrumentationRegistry.getInstrumentation().targetContext.getString(id)

    private class Fixture(private val supportsAI: Boolean = false) : Dispatcher() {
        val emergency = AtomicReference(false)
        val toggle = AtomicReference<JSONObject>()
        val policy = AtomicReference<JSONObject>()
        val retry = AtomicReference<String>()

        private fun row(id: Int, name: String, state: String): JSONObject {
            val saved = policy.get()?.optJSONObject("policy").takeIf { id == 1 }
            return JSONObject().put("camera_id", id).put("name", name)
                .put("protocol", "onvif").put("events_enabled", true).put("recording_enabled", state != "paused")
                .put("policy", saved?.let { JSONObject(it.toString()).put("revision", 5) } ?: JSONObject()
                    .put("revision", 4).put("mode", if (state == "linked_source_fault") "linked" else "continuous")
                    .put("source", "none").put("profile", if (state == "linked_source_fault") "economical" else "none")
                    .put("linked_camera_id", if (id == 2) 1 else JSONObject.NULL))
                .put("runtime", JSONObject().put("actual_status", state).put("legacy_recording_mode", "always").put("ai_available", supportsAI).apply {
                    if (state == "linked_source_fault") {
                        put("retry_camera_id", 1); put("emergency_requested", true)
                        put("incident", JSONObject().put("reason", "timeout").put("failed_attempts", 5))
                    }
                })
        }

        override fun dispatch(request: RecordedRequest): MockResponse {
            val path = request.path.orEmpty()
            val response = when {
                path == "/api/ai-recording/cameras" -> JSONObject().put("cameras", JSONArray()
                    .put(row(1, "Garden", "paused")).put(row(2, "Gate", "linked_source_fault")))
                path == "/api/ai-recording/cameras/1" && request.method == "PUT" -> {
                    policy.set(JSONObject(request.body.readUtf8())); row(1, "Garden", "paused")
                }
                path == "/api/ai-recording/emergency" -> {
                    if (request.method == "PUT") {
                        val body = JSONObject(request.body.readUtf8())
                        toggle.set(body); emergency.set(body.getBoolean("enabled"))
                    }
                    JSONObject().put("enabled", emergency.get())
                }
                path == "/api/ai-recording/cameras/1/retry" && request.method == "POST" -> {
                    retry.set(path); JSONObject().put("status", "queued")
                }
                else -> return MockResponse().setResponseCode(404)
            }
            return MockResponse().setHeader("Content-Type", "application/json").setBody(response.toString())
        }
    }

    private fun screen(server: MockWebServer, canSettings: Boolean, messages: (String) -> Unit = {}) {
        val api = NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("local-fixture") }
        compose.setContent { KMVMSTheme { RecordingCenterScreen(api, canSettings, messages) } }
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("recording-edit-1").fetchSemanticsNodes().isNotEmpty() }
    }

    @Test fun defaultDisabledAndEmergencyUpdateIsSharedAndRevisionGuarded() = MockWebServer().use { server ->
        val fixture = Fixture(); server.dispatcher = fixture
        screen(server, true)
        compose.waitUntil(10_000) { compose.onAllNodes(hasTestTag("recording-emergency") and isEnabled()).fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("recording-emergency").assertIsOff().performClick()
        compose.waitUntil(10_000) { fixture.toggle.get() != null }
        assertFalse(fixture.toggle.get().getBoolean("expected_enabled"))
        assertTrue(fixture.toggle.get().getBoolean("enabled"))
        compose.waitUntil(10_000) { compose.onAllNodes(hasTestTag("recording-emergency") and isOn()).fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("recording-status-1").assertTextEquals(str(R.string.rec_status_paused))
        compose.onNodeWithTag("recording-status-2").assertTextEquals(str(R.string.rec_status_linked_source_fault))
        Unit
    }

    @Test fun cameraEventsCanBeSavedWithoutUnpausingAndLinkToDependentsIsNotOffered() = MockWebServer().use { server ->
        val fixture = Fixture(); server.dispatcher = fixture
        screen(server, false)
        compose.onNodeWithTag("recording-emergency").assertDoesNotExist()
        compose.onNodeWithTag("recording-edit-1").performClick()
        compose.onNodeWithTag("recording-mode").performClick()
        compose.onNode(hasText(str(R.string.rec_m_linked)) and hasClickAction()).performClick()
        compose.onNodeWithTag("recording-source").assertIsNotEnabled()
        compose.onNodeWithTag("recording-save").assertIsNotEnabled()
        compose.onNodeWithTag("recording-mode").performClick()
        compose.onNode(hasText(str(R.string.rec_m_events)) and hasClickAction()).performClick()
        compose.onNodeWithTag("recording-profile").performClick()
        compose.onNode(hasText(str(R.string.rec_p_balanced)) and hasClickAction()).performClick()
        compose.onNodeWithTag("recording-save").performClick()
        compose.waitUntil(10_000) { fixture.policy.get() != null }
        val sent = fixture.policy.get()
        assertEquals(4, sent.getInt("expected_revision"))
        val policy = sent.getJSONObject("policy")
        assertEquals("events", policy.getString("mode"))
        assertEquals("camera", policy.getString("source"))
        assertEquals("balanced", policy.getString("profile"))
        assertEquals(30, policy.getInt("prebuffer_seconds"))
        assertFalse(policy.has("enabled"))
        compose.waitUntil(10_000) { compose.onAllNodesWithTag("recording-editor").fetchSemanticsNodes().isEmpty() }
        compose.onNodeWithTag("recording-status-1").assertTextEquals(str(R.string.rec_status_paused))
        Unit
    }

    @Test fun linkedFaultExplainsPendingProtectionAndRetriesTheActualSource() = MockWebServer().use { server ->
        val fixture = Fixture(); server.dispatcher = fixture
        val message = AtomicReference<String>()
        screen(server, true, message::set)
        compose.onNodeWithTag("recording-details-2").performClick()
        compose.onNodeWithText(str(R.string.rec_reason_timeout)).assertIsDisplayed()
        compose.onNodeWithText(str(R.string.rec_emergency_pending)).assertIsDisplayed()
        compose.onNodeWithTag("recording-retry").performClick()
        compose.waitUntil(10_000) { message.get() != null }
        assertEquals("/api/ai-recording/cameras/1/retry", fixture.retry.get())
        assertEquals(str(R.string.rec_retry_queued), message.get())
    }

    @Test fun nasAiCanBeSelectedAndSavedWithoutTurningOnSceneChangeOrEconomy() = MockWebServer().use { server ->
        val fixture = Fixture(supportsAI = true); server.dispatcher = fixture
        screen(server, false)
        compose.onNodeWithTag("recording-edit-1").performClick()
        compose.onNodeWithTag("recording-mode").performClick()
        compose.onNode(hasText(str(R.string.rec_m_events_choose)) and hasClickAction()).performClick()
        compose.onNodeWithTag("recording-analytics").performClick()
        compose.onNode(hasText(str(R.string.rec_a_ai)) and hasClickAction()).performClick()
        compose.onNodeWithTag("recording-profile").performClick()
        compose.onNode(hasText(str(R.string.rec_p_economical)) and hasClickAction()).assertDoesNotExist()
        compose.onNode(hasText(str(R.string.rec_p_full_quality)) and hasClickAction()).performClick()
        compose.onNodeWithTag("recording-save").performClick()
        compose.waitUntil(10_000) { fixture.policy.get() != null }
        val policy = fixture.policy.get().getJSONObject("policy")
        assertEquals("ai", policy.getString("source"))
        assertEquals("full_quality", policy.getString("profile"))
        assertFalse(policy.getJSONArray("event_kinds").toString().contains("motion"))
    }
}
