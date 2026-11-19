package ru.kmvms.mobile.ui.chronology

import android.content.res.Configuration
import androidx.compose.runtime.*
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.media3.common.util.UnstableApi
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import okhttp3.mockwebserver.*
import okio.Buffer
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.chronology.*
import ru.kmvms.mobile.core.*
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.ui.KMVMSTheme
import java.time.ZoneOffset
import java.time.LocalDateTime
import kotlin.math.abs

@UnstableApi
@RunWith(AndroidJUnit4::class)
class TimelineCameraSwipeTest {
    @get:Rule val compose=createComposeRule()

    private fun cycle(landscape:Boolean, withVideo:Boolean=false) = MockWebServer().use { server ->
        val first=1_700_000_000_000L
        val bytes=if(withVideo) InstrumentationRegistry.getInstrumentation().context.assets.open("camera-swipe-fixture.mp4").use{it.readBytes()} else byteArrayOf()
        server.dispatcher=object:Dispatcher(){override fun dispatch(request:RecordedRequest):MockResponse {
            return when(request.requestUrl!!.encodedPath) {
                "/api/chronology/ranges"->MockResponse().setBody("""{"timezone":{"id":"UTC"},"items":{}}""")
                "/api/chronology/playback"->{
                    val camera=request.requestUrl!!.queryParameter("camera_id")!!.toLong()
                    if(!withVideo || camera==102L) MockResponse().setBody("""{"has_video":false,"availability_status":"no_recording"}""")
                    else {
                        val at=LocalDateTime.parse(request.requestUrl!!.queryParameter("ts")).toInstant(ZoneOffset.UTC).toEpochMilli()
                        MockResponse().setBody("""{"has_video":true,"camera_id":$camera,"segment_id":$camera,"archive_root_id":"fixture","duration_sec":30,"offset_sec":${(at-first)/1000.0},"mime_type":"video/mp4"}""")
                    }
                }
                "/api/chronology/media-token"->MockResponse().setBody("""{"media_token":"fixture-media","expires_in":300}""")
                "/api/chronology/file"->{
                    val range=Regex("bytes=(\\d+)-(\\d*)").matchEntire(request.getHeader("Range").orEmpty())
                    val start=range?.groupValues?.get(1)?.toInt() ?: 0
                    val end=range?.groupValues?.get(2)?.toIntOrNull()?.coerceAtMost(bytes.lastIndex) ?: bytes.lastIndex
                    if(start>bytes.lastIndex) MockResponse().setResponseCode(416)
                    else MockResponse().setResponseCode(if(range==null)200 else 206).setHeader("Content-Type","video/mp4")
                        .setHeader("Accept-Ranges","bytes").setHeader("Content-Range","bytes $start-$end/${bytes.size}")
                        .setBody(Buffer().write(bytes.copyOfRange(start,end+1)))
                }
                else->MockResponse().setResponseCode(404)
            }
        }}
        server.start()
        val api=NasApi(ServerAddress.parse(server.url("/").toString())).apply{useSession("fixture")}
        lateinit var controller:ChronologyController
        var id by mutableLongStateOf(103)
        val selected=listOf(103L,101L,102L)
        var showing by mutableStateOf(true)
        compose.setContent {
            if (showing) {
            val context=LocalContext.current
            controller=remember{ChronologyController(context,ChronologyApi(api,ZoneOffset.UTC),first).apply{select(selected);play(true)}}
            val state by controller.state.collectAsStateWithLifecycle()
            val config=Configuration(LocalConfiguration.current).apply{orientation=if(landscape)Configuration.ORIENTATION_LANDSCAPE else Configuration.ORIENTATION_PORTRAIT}
            DisposableEffect(controller){onDispose{controller.close()}}
            CompositionLocalProvider(LocalConfiguration provides config){KMVMSTheme{key(id){
                TimelineFullscreen(controller,state,id,onCameraSwipe={step->adjacentCamera(state.selected,id,step)?.let{id=it}}){}
            }}}
            }
        }
        compose.waitUntil(12_000){controller.state.value.running &&
            controller.state.value.phases.values.count{it==TimelinePhase.EMPTY}==if(withVideo)1 else 3}
        lateinit var tracks:Map<Long,TimelineTrack>
        var before=0L
        compose.runOnIdle {
            tracks=selected.associateWith{controller.track(it)!!}
            before=controller.state.value.position
        }
        fun swipe(next:Boolean) {
            val current=compose.runOnIdle{id}
            val node=if(withVideo && current!=102L) "video-gesture-surface" else "timeline-empty-$current"
            compose.onNodeWithTag(node).performTouchInput {
                swipe(Offset(width*(if(next).8f else .2f),height*.5f),Offset(width*(if(next).2f else .8f),height*.5f),300)
            }
        }
        swipe(true);compose.onNodeWithTag("timeline-fullscreen-101").assertIsDisplayed()
        swipe(false);compose.onNodeWithTag("timeline-fullscreen-103").assertIsDisplayed()
        swipe(false);compose.onNodeWithTag("timeline-fullscreen-102").assertIsDisplayed()
        swipe(true);compose.onNodeWithTag("timeline-fullscreen-103").assertIsDisplayed()
        compose.runOnIdle {
            val state=controller.state.value
            assertTrue("swiping must not pause the common clock",state.wantsPlay)
            assertTrue(state.running)
            assertTrue(state.position>=before)
            assertTrue(state.position<before+15_000)
            assertEquals(selected,state.selected)
            tracks.forEach{(camera,track)->assertSame("must not select/seek/recreate archive tracks",track,controller.track(camera))}
            if(withVideo) {
                assertTrue(tracks.getValue(103).player.isPlaying)
                assertTrue(tracks.getValue(101).player.isPlaying)
                assertTrue("archive players must retain the shared clock",abs(tracks.getValue(103).player.currentPosition-tracks.getValue(101).player.currentPosition)<400)
            }
        }
        compose.runOnIdle { showing=false }
    }
    @Test fun portraitCyclesWithoutPausingOrRecreatingTheSynchronizedGroup()=cycle(false)
    @Test fun landscapeCyclesAndDoesNotGetStuckOnACameraWithNoRecording()=cycle(true)
    @Test fun decodedArchiveVideoKeepsPlayingInSyncWhileSwipingPastAnEmptyCamera()=cycle(false,true)
}
