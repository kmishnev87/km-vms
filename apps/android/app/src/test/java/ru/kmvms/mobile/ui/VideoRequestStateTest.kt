package ru.kmvms.mobile.ui

import androidx.compose.runtime.saveable.SaverScope
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.events.MobileVideoRequest

class VideoRequestStateTest {
    private val scope=object:SaverScope { override fun canBeSaved(value:Any)=true }
    @Test fun eventOriginAndPreviewModeSurviveActivityRecreationWithoutChangingArchiveLaunches() {
        val requests=listOf(MobileVideoRequest(1,"Panorama",returnToEvents=true),
            MobileVideoRequest(2,"PTZ",singleCamera=true),MobileVideoRequest(3,"Gate",archiveTime=1_700_000_000_000))
        requests.forEach { request ->
            val saved=with(videoRequestSaver) { scope.save(request) }
            assertEquals(request,videoRequestSaver.restore(requireNotNull(saved)))
        }
        val old=listOf<Any>(1L,"Panorama",-1L,"old-key",false)
        assertFalse(videoRequestSaver.restore(old)!!.returnToEvents)
    }
}
