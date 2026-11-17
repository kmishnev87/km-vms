package ru.kmvms.mobile.chronology

import androidx.media3.common.util.UnstableApi
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.core.TimelinePhase
import ru.kmvms.mobile.data.NasApi
import java.time.ZoneOffset
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@UnstableApi
@RunWith(AndroidJUnit4::class)
class ChronologyLookupConcurrencyTest {
    @Test fun fiveCamerasReachTheServerBeforeTheFirstAnswerAndEmptyTracksDoNotBlockPlayback() {
        val entered = CountDownLatch(5); val release = CountDownLatch(1)
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        MockWebServer().use { server ->
            server.dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    val url = request.requestUrl!!
                    return when (url.encodedPath) {
                        "/api/chronology/ranges" -> MockResponse().setBody("""{"timezone":{"id":"UTC"},"items":{}}""")
                        "/api/chronology/playback" -> {
                            entered.countDown()
                            check(release.await(5, TimeUnit.SECONDS))
                            MockResponse().setBody("""{"camera_id":${url.queryParameter("camera_id")},"has_video":false,"availability_status":"no_recording"}""")
                        }
                        else -> MockResponse().setResponseCode(404)
                    }
                }
            }
            server.start()
            val api = NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }
            lateinit var controller: ChronologyController
            instrumentation.runOnMainSync {
                controller = ChronologyController(instrumentation.targetContext, ChronologyApi(api, ZoneOffset.UTC),
                    initialPosition = 1_700_000_000_000, initialZone = ZoneOffset.UTC)
                controller.select((1L..5L).toList()); controller.play(true)
            }
            try {
                assertTrue("all five lookups must reach HTTP without waiting for a three-wide batch", entered.await(4, TimeUnit.SECONDS))
                release.countDown()
                val deadline = System.nanoTime() + 4_000_000_000
                while (controller.state.value.phases.values.count { it == TimelinePhase.EMPTY } != 5 && System.nanoTime() < deadline) Thread.sleep(20)
                assertEquals(5, controller.state.value.phases.values.count { it == TimelinePhase.EMPTY })
                assertTrue(controller.state.value.running)
                assertTrue(controller.state.value.waiting.isEmpty())
            } finally {
                release.countDown()
                instrumentation.runOnMainSync { controller.close() }
            }
        }
    }
}
