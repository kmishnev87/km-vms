package ru.kmvms.mobile.ui.chronology

import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.data.Camera
import ru.kmvms.mobile.ui.KMVMSTheme
import ru.kmvms.mobile.ui.live.OrderedCameraPicker

@RunWith(AndroidJUnit4::class)
class TimelineSelectionTest {
    @get:Rule val compose=createComposeRule()
    private val cameras=listOf(101L to "Panorama",102L to "PTZ",103L to "Retained camera").map{(id,name)->
        Camera(id,name,false,"onvif","","",true,null,null,null,"always",deleted=id==103L)
    }
    @Test fun allIncludesOnlyCurrentCamerasAndRetainedArchivesRemainManuallySelectable() {
        var applied:List<Long>?=null
        compose.setContent { KMVMSTheme { OrderedCameraPicker(cameras,emptyList(),{},true,"timeline"){applied=it} } }
        compose.onNodeWithTag("timeline-retained-divider").assertIsDisplayed()
        compose.onNodeWithTag("timeline-picker-all").performClick()
        compose.onNodeWithTag("timeline-picker-order-103",useUnmergedTree=true).onChildren().assertCountEquals(0)
        compose.onNodeWithTag("timeline-picker-apply").performClick()
        compose.runOnIdle { assertEquals(listOf(101L,102L),applied) }
        compose.onNodeWithTag("timeline-picker-camera-103").performClick()
        compose.onNodeWithTag("timeline-picker-order-103",useUnmergedTree=true).onChild().assertTextEquals("3")
        compose.onNodeWithTag("timeline-picker-apply").performClick()
        compose.runOnIdle { assertEquals(listOf(101L,102L,103L),applied) }
    }
    @Test fun selectionOrderAndLongPressReorderingWorkLikeLive() {
        var applied:List<Long>?=null
        compose.setContent { KMVMSTheme { OrderedCameraPicker(cameras,emptyList(),{},true,"timeline"){applied=it} } }
        compose.onNodeWithTag("timeline-picker-camera-102").performClick()
        compose.onNodeWithTag("timeline-picker-camera-101").performClick()
        compose.onNodeWithTag("timeline-picker-order-102",useUnmergedTree=true).onChild().assertTextEquals("1")
        compose.onNodeWithTag("timeline-picker-order-101",useUnmergedTree=true).onChild().assertTextEquals("2")
        compose.onNodeWithTag("timeline-picker-drag-101").performTouchInput {
            down(center);advanceEventTime(700);moveBy(Offset(0f,-85f),500);up()
        }
        compose.onNodeWithTag("timeline-picker-apply").performClick()
        compose.runOnIdle { assertEquals(listOf(101L,102L),applied) }
    }
}
