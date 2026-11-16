package ru.kmvms.mobile.ui.live

import org.junit.Assert.assertEquals
import org.junit.Test
import ru.kmvms.mobile.events.MobileVideoRequest

class LiveBackActionTest {
    @Test fun eventLiveExitsToOriginInPortraitAndFullscreen() {
        val event=MobileVideoRequest(1,"Panorama",returnToEvents=true)
        for(fullscreen in listOf(false,true)) assertEquals(LiveBackAction.EXIT_TO_ORIGIN,liveBackAction(event,fullscreen))
    }
    @Test fun cameraPreviewStillLeavesFullscreenBeforeReturningToCameras() {
        val preview=MobileVideoRequest(1,"Panorama",singleCamera=true)
        assertEquals(LiveBackAction.LEAVE_FULLSCREEN,liveBackAction(preview,true))
        assertEquals(LiveBackAction.EXIT_TO_ORIGIN,liveBackAction(preview,false))
    }
    @Test fun regularLiveStillReturnsToItsOwnCameraList() {
        assertEquals(LiveBackAction.SHOW_CAMERA_LIST,liveBackAction(null,false))
        assertEquals(LiveBackAction.SHOW_CAMERA_LIST,liveBackAction(null,true))
    }
}
