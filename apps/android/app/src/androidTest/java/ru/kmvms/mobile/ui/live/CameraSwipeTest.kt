package ru.kmvms.mobile.ui.live

import android.content.Context
import android.content.res.Configuration
import androidx.compose.foundation.layout.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import androidx.media3.common.util.UnstableApi
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.RecordedRequest
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.core.*
import ru.kmvms.mobile.data.Camera
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.events.MobileVideoRequest
import ru.kmvms.mobile.media.*
import ru.kmvms.mobile.ui.KMVMSTheme

@UnstableApi
@RunWith(AndroidJUnit4::class)
class CameraSwipeTest {
    @get:Rule val compose = createComposeRule()
    private val cameras = listOf(101L, 102L, 103L).map { id ->
        Camera(id, "Camera $id", false, "onvif", "", "", true, null, null, null, "always")
    }
    private fun surface() = compose.onNodeWithTag("video-gesture-surface")
    private fun left() = surface().performTouchInput { swipe(Offset(width*.8f,height*.5f),Offset(width*.2f,height*.5f),300) }
    private fun right() = surface().performTouchInput { swipe(Offset(width*.2f,height*.5f),Offset(width*.8f,height*.5f),300) }

    @Test fun unscaledSwipesCycleAndVerticalOrShortGesturesDoNotNavigate() {
        var id by mutableLongStateOf(103)
        var taps = 0
        compose.setContent { KMVMSTheme { key(id) {
            VideoSurface(null,VideoState(),{},Modifier.fillMaxWidth().height(240.dp),zoomable=true,
                onTap={taps++},onCameraSwipe={step->adjacentCamera(listOf(103,101,102),id,step)?.let{id=it}})
        } } }
        left(); compose.runOnIdle { assertEquals(101,id) }
        right(); compose.runOnIdle { assertEquals(103,id) }
        right(); compose.runOnIdle { assertEquals(102,id); assertEquals(0,taps) }
        surface().performTouchInput { swipe(Offset(width*.5f,height*.8f),Offset(width*.5f,height*.2f),300) }
        surface().performTouchInput { swipe(Offset(width*.5f,height*.5f),Offset(width*.48f,height*.5f),300) }
        compose.runOnIdle { assertEquals(102,id); assertEquals(0,taps) }
    }

    @Test fun zoomedImagePansInsteadOfSwitchingAndTwoFingerGesturesNeverNavigate() {
        var switches = 0
        compose.setContent { KMVMSTheme { VideoSurface(null,VideoState(),{},Modifier.fillMaxWidth().height(240.dp),
            zoomable=true,onCameraSwipe={switches++}) } }
        surface().performTouchInput {
            pinch(Offset(width*.4f,height*.5f),Offset(width*.6f,height*.5f),
                Offset(width*.2f,height*.5f),Offset(width*.8f,height*.5f),500)
        }
        left(); right()
        compose.runOnIdle { assertEquals(0,switches) }
        surface().performTouchInput { doubleClick() }
        left()
        compose.runOnIdle { assertEquals(1,switches) }
    }

    @Test fun pointTapsAndAreaRectanglesCoexistWithHorizontalCameraSwipes() {
        var mode by mutableStateOf(VideoGestureMode.POINT)
        var switches = 0; var points = 0; var areas = 0
        compose.setContent { KMVMSTheme { VideoSurface(null,VideoState(),{},Modifier.fillMaxWidth().height(240.dp),
            zoomable=true,mode=mode,onPoint={_,_->points++},onArea={_,_,_,_->areas++},onCameraSwipe={switches++}) } }
        surface().performTouchInput { click() }; left()
        compose.runOnIdle { assertEquals(1,points); assertEquals(1,switches); mode=VideoGestureMode.AREA }
        surface().performTouchInput { swipe(Offset(width*.25f,height*.25f),Offset(width*.75f,height*.75f),400) }
        compose.runOnIdle { assertEquals(1,areas); assertEquals(1,switches) }
        right()
        compose.runOnIdle { assertEquals(1,areas); assertEquals(2,switches) }
    }

    private fun live(landscape: Boolean, single: Boolean = false) = MockWebServer().use { server ->
        server.dispatcher=object:Dispatcher(){override fun dispatch(request:RecordedRequest)=MockResponse().setResponseCode(404)}
        server.start()
        val api=NasApi(ServerAddress.parse(server.url("/").toString())).apply{useSession("fixture")}
        val context=ApplicationProvider.getApplicationContext<Context>()
        val profile="test-swipe-$landscape-$single"
        val preferences=LiveViewPreferences(context)
        preferences.save(profile,listOf(103,101,102))
        var immersive=false
        var showing by mutableStateOf(true)
        try {
            compose.setContent {
                if (showing) {
                val config=Configuration(LocalConfiguration.current).apply{orientation=if(landscape)Configuration.ORIENTATION_LANDSCAPE else Configuration.ORIENTATION_PORTRAIT}
                CompositionLocalProvider(LocalConfiguration provides config) { KMVMSTheme {
                    LiveScreen(api,cameras,emptyMap(),{}, {immersive=it},
                        initial=MobileVideoRequest(cameraId=103,cameraName="Camera 103",singleCamera=single),profileId=profile)
                } }
                }
            }
            compose.onNodeWithTag("live-detail-103").assertIsDisplayed()
            left()
            if(single) compose.onNodeWithTag("live-detail-103").assertIsDisplayed()
            else {
                compose.onNodeWithTag("live-detail-101").assertIsDisplayed()
                right(); compose.onNodeWithTag("live-detail-103").assertIsDisplayed()
                right(); compose.onNodeWithTag("live-detail-102").assertIsDisplayed()
            }
            compose.runOnIdle { assertEquals(landscape,immersive) }
        } finally {
            compose.runOnIdle { showing=false }
            context.getSharedPreferences("nas_profiles",Context.MODE_PRIVATE).edit().remove("live_order.$profile").apply()
        }
    }
    @Test fun livePortraitUsesTheSavedOrderAndStaysInDetail() = live(false)
    @Test fun liveLandscapeKeepsFullscreenAcrossTheCycle() = live(true)
    @Test fun singlePreviewDoesNotIntroduceUnselectedCameras() = live(false,true)

    @Test fun changingLiveFocusReleasesPreviousVideoAndResetsItsCallAndPtzSelection() = MockWebServer().use { server ->
        server.dispatcher=object:Dispatcher(){override fun dispatch(request:RecordedRequest)=MockResponse().setResponseCode(404)}
        server.start()
        val api=NasApi(ServerAddress.parse(server.url("/").toString()))
        lateinit var controller:LiveController
        compose.setContent {
            val context=LocalContext.current
            controller=remember{LiveController(context,api).apply{cameras(cameras);focus(101)}}
            DisposableEffect(controller){onDispose{controller.close()}}
        }
        compose.runOnIdle {
            controller.ptz(101,true);controller.talk(101,true);controller.sound(101)
            val old=controller.sessions.value.getValue(101)
            controller.focus(102)
            assertEquals(setOf(102L),controller.sessions.value.keys)
            assertNotSame(old,controller.sessions.value[102])
            val previous=controller.playback.value.getValue(101)
            assertFalse(previous.ptz);assertFalse(previous.talk);assertFalse(previous.sound)
            controller.focus(101)
            assertEquals(setOf(101L),controller.sessions.value.keys)
            assertFalse(controller.playback.value.getValue(101).talk)
        }
    }
}
