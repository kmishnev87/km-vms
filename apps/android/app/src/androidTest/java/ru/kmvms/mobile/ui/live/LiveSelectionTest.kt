package ru.kmvms.mobile.ui.live

import androidx.compose.runtime.*
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import android.content.Context
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.data.Camera
import ru.kmvms.mobile.media.LiveViewPreferences
import ru.kmvms.mobile.ui.KMVMSTheme

@RunWith(AndroidJUnit4::class)
class LiveSelectionTest {
    @get:Rule val compose = createComposeRule()
    private val cameras = listOf(101L to "Panorama", 102L to "PTZ", 103L to "Gate").map { (id, name) ->
        Camera(id, name, false, "onvif", "", "", true, null, null, null, "always")
    }

    @Test fun selectionNumbersFollowTapsAndAreNotAppliedUntilConfirmed() {
        var applied: List<Long>? = null
        compose.setContent { KMVMSTheme { LiveCameraPicker(cameras, emptyList(), {}, { applied = it }) } }
        compose.onNodeWithTag("live-picker-camera-103").performClick()
        compose.onNodeWithTag("live-picker-camera-101").performClick()
        compose.onNodeWithTag("live-picker-order-103", useUnmergedTree = true).onChild().assertTextEquals("1")
        compose.onNodeWithTag("live-picker-order-101", useUnmergedTree = true).onChild().assertTextEquals("2")
        compose.runOnIdle { assertNull(applied) }
        compose.onNodeWithTag("live-picker-camera-103").performClick()
        compose.onNodeWithTag("live-picker-order-101", useUnmergedTree = true).onChild().assertTextEquals("1")
        compose.onNodeWithTag("live-picker-apply").performClick()
        compose.runOnIdle { assertEquals(listOf(101L), applied) }
    }

    @Test fun longPressDragChangesOrderWithoutDroppingSelection() {
        var applied: List<Long>? = null
        compose.setContent { KMVMSTheme { LiveCameraPicker(cameras, listOf(101, 102, 103), {}, { applied = it }) } }
        compose.onNodeWithTag("live-picker-drag-103").performTouchInput {
            down(center); advanceEventTime(700); moveBy(androidx.compose.ui.geometry.Offset(0f, -150f), 500); up()
        }
        compose.onNodeWithTag("live-picker-apply").performClick()
        compose.runOnIdle { assertNotNull(applied); assertEquals(setOf(101L,102L,103L), applied!!.toSet()); assertTrue(applied!!.indexOf(103L) < 2) }
    }

    @Test fun storedOrderAndEmptySelectionAreSeparateForEachNas() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val preferences = LiveViewPreferences(context)
        val first = "test-live-order-first"
        val second = "test-live-order-second"
        try {
            preferences.save(first, listOf(103, 101)); preferences.save(second, emptyList())
            assertEquals(listOf(103L,101L), LiveViewPreferences(context).load(first))
            assertEquals(emptyList<Long>(), LiveViewPreferences(context).load(second))
            assertNull(preferences.load("test-live-order-new"))
        } finally {
            context.getSharedPreferences("nas_profiles", Context.MODE_PRIVATE).edit().remove("live_order.$first").remove("live_order.$second").apply()
        }
    }
}
