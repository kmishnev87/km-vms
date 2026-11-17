package ru.kmvms.mobile.ui.live

import android.Manifest
import android.content.res.Configuration
import android.graphics.Bitmap
import androidx.compose.foundation.layout.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalResources
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okio.ByteString
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.core.CameraPlayback
import ru.kmvms.mobile.core.LiveTransport
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.data.Camera
import ru.kmvms.mobile.media.LiveController
import ru.kmvms.mobile.ptz.PtzCapabilities
import ru.kmvms.mobile.talk.CallPhase
import ru.kmvms.mobile.talk.TalkController
import ru.kmvms.mobile.ui.KMVMSTheme
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicInteger
import java.util.Locale

@RunWith(AndroidJUnit4::class)
class CameraCallTest {
    @get:Rule val compose = createComposeRule()

    private class Owner : LifecycleOwner {
        val registry = LifecycleRegistry(this)
        override val lifecycle: Lifecycle get() = registry
    }

    private class Fixture(val fullDuplex: Boolean = false, val needsCredential: Boolean = false,
                          val respondToHeartbeat: Boolean = true) : java.io.Closeable {
        val server = MockWebServer()
        val frames = CopyOnWriteArrayList<ByteString>()
        val requests = CopyOnWriteArrayList<RecordedRequest>()
        val ended = AtomicInteger()
        lateinit var peer: WebSocket
        val api: NasApi
        init {
            server.dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    requests += request
                    return when {
                        request.path!!.endsWith("/audio") -> MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                            override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                                peer = webSocket
                                webSocket.send("""{"type":"ready"}""")
                            }
                            override fun onMessage(webSocket: WebSocket, bytes: ByteString) { frames += bytes }
                            override fun onMessage(webSocket: WebSocket, text: String) {
                                if (respondToHeartbeat && text.contains("keepalive")) webSocket.send("""{"type":"keepalive"}""")
                            }
                        })
                        request.method == "DELETE" -> { ended.incrementAndGet(); MockResponse().setBody("{}") }
                        request.path == "/api/talk/sessions" -> MockResponse().setBody("""{"session_id":"fixture-session-0123456789","media_token":"fixture-scoped-media"}""")
                        else -> MockResponse().setBody("""{"status":"${if (needsCredential) "needs_credentials" else "supported"}","duplex":"${if (fullDuplex) "full" else "push_to_talk"}","credential_required":$needsCredential,"credential_saved":false}""")
                    }
                }
            }
            server.start()
            api = NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture-access") }
        }
        override fun close() = server.close()
    }

    @Test fun callAlwaysSelectsFastVideoWithoutChangingUserSoundOrPtz() {
        val first = CameraPlayback(1, sound = false)
        assertEquals(LiveTransport.WEBSOCKET_FMP4, first.copy(talk = true).transport)
        assertFalse(first.copy(talk = true).sound)
        assertEquals(LiveTransport.WEBSOCKET_FMP4, first.copy(ptz = true, talk = false).transport)
        assertEquals(LiveTransport.HLS, first.copy(talk = false).transport)
    }

    @Test fun standardTalkUsesHoldControlAndScopedSocketAuthorizationThenReleasesOnPause() = Fixture().use { fixture ->
        lateinit var talk: TalkController
        lateinit var owner: Owner
        var active = false
        compose.setContent {
            val context = LocalContext.current
            owner = remember { Owner().apply { registry.currentState = Lifecycle.State.RESUMED } }
            talk = remember { TalkController(context, fixture.api, 7) }
            CompositionLocalProvider(LocalLifecycleOwner provides owner) {
                KMVMSTheme {
                    CameraCallLifecycle(talk) { enabled, _ -> active = enabled }
                    Column(Modifier.width(320.dp)) { CallButton(talk); CallStatus(talk) }
                }
            }
        }
        compose.waitUntil(8000) { talk.capability.value.available }
        compose.runOnIdle { talk.start() }
        compose.waitUntil(8000) { talk.state.value.phase == CallPhase.ACTIVE }
        compose.onNodeWithTag("call-hold").assertIsDisplayed()
        compose.runOnIdle { assertTrue(active); assertFalse(talk.state.value.microphone) }
        val request = fixture.requests.single { it.path!!.endsWith("/audio") }
        assertEquals("Bearer fixture-scoped-media", request.getHeader("Authorization"))
        assertFalse(request.path!!.contains("token"))
        compose.runOnIdle { owner.registry.currentState = Lifecycle.State.STARTED }
        compose.waitUntil(5000) { talk.state.value.phase == CallPhase.IDLE && fixture.ended.get() == 1 }
        compose.runOnIdle { assertFalse(active) }
        assertTrue(fixture.frames.isEmpty())
    }

    @Test fun fullDuplexCapturesOnlyWhileEnabledAndStopsOnHangup() = Fixture(fullDuplex = true).use { fixture ->
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        instrumentation.uiAutomation.executeShellCommand("pm grant ${instrumentation.targetContext.packageName} ${Manifest.permission.RECORD_AUDIO}").close()
        lateinit var talk: TalkController
        compose.setContent {
            val context = LocalContext.current
            talk = remember { TalkController(context, fixture.api, 7) }
            KMVMSTheme { Column(Modifier.width(320.dp)) { CameraCallLifecycle(talk) { _, _ -> }; CallButton(talk); CallStatus(talk) } }
        }
        compose.waitUntil(8000) { talk.capability.value.available }
        compose.runOnIdle { talk.start() }
        compose.waitUntil(8000) { talk.state.value.phase in setOf(CallPhase.ACTIVE, CallPhase.FAILED) }
        compose.runOnIdle { assertEquals(talk.state.value.toString(), CallPhase.ACTIVE, talk.state.value.phase) }
        compose.waitUntil(8000) { fixture.frames.isNotEmpty() || talk.state.value.phase != CallPhase.ACTIVE }
        compose.runOnIdle { assertTrue(talk.state.value.toString(), talk.state.value.microphone) }
        assertTrue(talk.state.value.toString(), fixture.frames.isNotEmpty())
        assertEquals(640, fixture.frames.first().size)
        compose.onNodeWithTag("call-microphone").performClick()
        compose.runOnIdle { assertFalse(talk.state.value.microphone) }
        compose.onNodeWithTag("call-hangup").performClick()
        compose.waitUntil(5000) { fixture.ended.get() == 1 }
        compose.runOnIdle { assertEquals(CallPhase.IDLE, talk.state.value.phase) }
    }

    @Test fun separateCredentialIsRequestedBeforeAnyCallOrMicrophone() = Fixture(needsCredential = true).use { fixture ->
        lateinit var talk: TalkController
        compose.setContent {
            val context = LocalContext.current
            talk = remember { TalkController(context, fixture.api, 7) }
            KMVMSTheme { CallButton(talk) }
        }
        compose.waitUntil(8000) { talk.capability.value.available }
        compose.onNodeWithTag("live-call").performClick()
        compose.onNodeWithTag("call-password").assertIsDisplayed()
        assertTrue(fixture.requests.none { it.path == "/api/talk/sessions" })
        assertFalse(talk.state.value.microphone)
        compose.runOnIdle { talk.close() }
    }

    @Test fun silentBrokenConnectionEndsEvenWithMicrophoneOff() = Fixture(respondToHeartbeat = false).use { fixture ->
        lateinit var talk: TalkController
        compose.setContent {
            val context = LocalContext.current
            talk = remember { TalkController(context, fixture.api, 7) }
            KMVMSTheme { CameraCallLifecycle(talk) { _, _ -> }; CallButton(talk); CallStatus(talk) }
        }
        compose.waitUntil(8000) { talk.capability.value.available }
        compose.runOnIdle { talk.start() }
        compose.waitUntil(8000) { talk.state.value.phase == CallPhase.ACTIVE }
        compose.waitUntil(9000) { talk.state.value.phase == CallPhase.FAILED }
        compose.runOnIdle { assertFalse(talk.state.value.microphone); assertEquals("connection_lost", talk.state.value.reason) }
        compose.waitUntil(5000) { fixture.ended.get() == 1 }
    }

    @androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
    @Test fun narrowFullscreenKeepsCallPtzSoundStreamAndHangupVisibleInAllLanguages() = Fixture().use { fixture ->
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        instrumentation.uiAutomation.executeShellCommand("pm grant ${instrumentation.targetContext.packageName} ${Manifest.permission.RECORD_AUDIO}").close()
        val camera = Camera(7, "Test camera", false, "onvif", "192.0.2.7", "offline", true, 5, 7, 10, "continuous")
        var language by mutableStateOf("en")
        lateinit var controller: LiveController
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        compose.setContent {
            val configuration = Configuration(LocalConfiguration.current).apply { setLocale(Locale.forLanguageTag(language)) }
            val localized = context.createConfigurationContext(configuration)
            controller = remember { LiveController(context, fixture.api).apply { cameras(listOf(camera)) } }
            CompositionLocalProvider(LocalResources provides localized.resources, LocalConfiguration provides configuration) {
                KMVMSTheme {
                    Box(Modifier.requiredSize(320.dp, 180.dp).testTag("call-layout")) {
                        LiveCameraDetail(camera, PtzCapabilities(true, "fixture", true, true, true, true, true),
                            controller, immersive = true, fullscreen = {}, close = {}, message = {}, mayTalk = true)
                    }
                }
            }
        }
        compose.waitForIdle()
        compose.waitUntil(8000) { fixture.requests.any { it.path == "/api/talk/cameras/7" } }
        compose.onNodeWithTag("live-call").assertIsEnabled()
        compose.onNodeWithTag("live-call").performClick()
        compose.waitUntil(8000) { compose.onAllNodesWithTag("call-hold").fetchSemanticsNodes().isNotEmpty() }
        for (locale in listOf("en", "ru", "zh-CN")) {
            compose.runOnIdle { language = locale }
            for (tag in listOf("live-ptz", "live-call", "live-sound", "live-stream", "call-hold", "call-hangup")) {
                compose.onNodeWithTag(tag).assertIsDisplayed()
                val bounds = compose.onNodeWithTag(tag).fetchSemanticsNode().boundsInRoot
                val area = compose.onNodeWithTag("call-layout").fetchSemanticsNode().boundsInRoot
                assertTrue("$locale/$tag left", bounds.left >= area.left)
                assertTrue("$locale/$tag right", bounds.right <= area.right)
            }
            val output = java.io.File(context.getExternalFilesDir(null), "camera-call-$locale.png")
            output.outputStream().use { compose.onNodeWithTag("call-layout").captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it) }
        }
        // Optional evidence handoff before the connected-test runner removes
        // its own debug application and output directory. Not a readiness wait.
        val capturePause = InstrumentationRegistry.getArguments().getString("kmvms.capturePauseMs")?.toLongOrNull() ?: 0L
        if (capturePause > 0) Thread.sleep(capturePause.coerceAtMost(45000L))
        compose.onNodeWithTag("call-hangup").performClick()
        compose.runOnIdle { controller.close() }
    }
}
