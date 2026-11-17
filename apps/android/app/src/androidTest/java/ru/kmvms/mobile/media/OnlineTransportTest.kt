package ru.kmvms.mobile.media

import androidx.media3.common.util.UnstableApi
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.*
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.core.CameraPlayback
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** Real Media3 decoder/data-source error path, without cameras, footage or a NAS. */
@UnstableApi
@RunWith(AndroidJUnit4::class)
class OnlineTransportTest {
    private fun withSource(code: String, action: (LivePlaybackSession,CountDownLatch,CountDownLatch,AtomicInteger,CountDownLatch)->Unit) {
        val hls=CountDownLatch(1);val secondSocket=CountDownLatch(2);val sockets=AtomicInteger()
        val instrumentation=InstrumentationRegistry.getInstrumentation()
        val scope=CoroutineScope(SupervisorJob()+Dispatchers.Main.immediate)
        val opened=CountDownLatch(1)
        MockWebServer().use { server ->
            server.dispatcher=object: Dispatcher(){
                override fun dispatch(request: RecordedRequest): MockResponse = when {
                    request.path=="/api/live/media-token" -> MockResponse().setBody("""{"media_token":"fixture","expires_in":300,"online_mode":"fast"}""")
                    request.path?.endsWith("/mse")==true -> MockResponse().withWebSocketUpgrade(object:WebSocketListener(){
                        override fun onOpen(socket:WebSocket,response:okhttp3.Response){sockets.incrementAndGet();secondSocket.countDown();opened.countDown();if(code!="hold")socket.send("""{"type":"error","code":"$code"}""")}
                    })
                    request.path=="/api/live/viewers" -> {hls.countDown();MockResponse().setBody("""{"viewer_id":"fixture-viewer","ok":true}""")}
                    request.path?.startsWith("/api/live/status")==true -> MockResponse().setBody("""{"items":[{"camera_id":1,"stream":"sub","ready":false}]}""")
                    request.method=="DELETE" -> MockResponse().setBody("""{"closed":true}""")
                    else -> MockResponse().setResponseCode(404)
                }
            }
            val api=NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }
            lateinit var session:LivePlaybackSession
            instrumentation.runOnMainSync {session=LivePlaybackSession(instrumentation.targetContext,api,CameraPlayback(1),scope)}
            try { action(session,hls,secondSocket,sockets,opened) }
            finally {instrumentation.runOnMainSync {session.close();scope.cancel()} }
        }
    }
    @Test fun confirmedIncompatibilityStartsHlsAndRetainsTheCaptionWhileItLoads() = withSource("mse_codec_unsupported") { session,hls,_,sockets,_ ->
        assertTrue("incompatible socket must open the HLS viewer",hls.await(15,TimeUnit.SECONDS))
        assertEquals(1,sockets.get())
        assertTrue(session.state.value.hlsFallback)
        assertFalse(session.state.value.unsupportedCodec)
    }
    @Test fun aNetworkFailureRetriesTheFastRouteWithoutClaimingIncompatibility() = withSource("mse_stream_failed") { session,hls,second,sockets,_ ->
        assertTrue("network failure must retry WebSocket",second.await(15,TimeUnit.SECONDS))
        assertTrue(sockets.get()>=2)
        assertEquals(1,hls.count)
        assertFalse(session.state.value.hlsFallback)
    }
    @Test fun ptzDoesNotRestartAnAlreadyFastStream() = withSource("hold") { session,_,second,sockets,opened ->
        assertTrue(opened.await(5,TimeUnit.SECONDS))
        InstrumentationRegistry.getInstrumentation().runOnMainSync {
            session.configure(CameraPlayback(1,ptz=true))
            session.configure(CameraPlayback(1,ptz=false))
        }
        assertFalse("PTZ toggle must retain the existing fast connection",second.await(1,TimeUnit.SECONDS))
        assertEquals(1,sockets.get())
    }
}
