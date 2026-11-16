package ru.kmvms.mobile.ui.events

import android.content.res.Configuration
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.events.*
import ru.kmvms.mobile.ui.KMVMSTheme
import java.util.Locale

@RunWith(AndroidJUnit4::class)
class EventPreferencesUiTest {
    @get:Rule val compose = createComposeRule()
    private val cameras = listOf(EventCamera(1, "Panorama / very long camera name", true), EventCamera(2, "Home", false))

    private fun settings(locale: String) {
        val value = mutableStateOf(EventPreferences(startMinute = 1140, endMinute = 420))
        compose.setContent {
            val config = Configuration(LocalConfiguration.current).apply { setLocale(Locale.forLanguageTag(locale)) }
            val context = LocalContext.current.createConfigurationContext(config)
            CompositionLocalProvider(LocalContext provides context, LocalConfiguration provides config) {
                KMVMSTheme { Column(Modifier.width(320.dp).fillMaxHeight().verticalScroll(rememberScrollState())) {
                    EventPersonalSettings(value.value, cameras, false) { value.value = it }
                } }
            }
        }
        compose.onNodeWithTag("events-hours-from").performScrollTo().assertIsDisplayed()
        compose.onNodeWithTag("events-schedule-per-camera").performScrollTo().performClick()
        compose.onNodeWithTag("events-pref-row-1").performScrollTo()
        val row = compose.onNodeWithTag("events-pref-row-1").getUnclippedBoundsInRoot()
        val from = compose.onNodeWithTag("events-camera-from-1").getUnclippedBoundsInRoot()
        val to = compose.onNodeWithTag("events-camera-to-1").getUnclippedBoundsInRoot()
        val arrow = compose.onNodeWithTag("events-camera-separator-1").getUnclippedBoundsInRoot()
        assertTrue(from.left >= row.left && to.right <= row.right)
        assertEquals(from.top, to.top)
        assertTrue(from.right - from.left <= 64.dp && to.right - to.left <= 64.dp)
        assertTrue(arrow.left >= from.right && arrow.right <= to.left)
        assertEquals((from.top + (from.bottom - from.top) / 2).value, (arrow.top + (arrow.bottom - arrow.top) / 2).value, .01f)
        assertTrue(row.bottom - row.top <= 48.dp)
        compose.onNodeWithTag("events-pref-camera-1").performClick()
        compose.onNodeWithTag("events-camera-from-1").assertIsNotEnabled()
        compose.runOnIdle { assertEquals(setOf(2L), value.value.cameraIds); assertTrue(value.value.perCameraHours) }
        compose.onNodeWithTag("events-schedule-all").performScrollTo().performClick()
        compose.onNodeWithTag("events-hours-from").assertTextContains("19:00", substring = true)
        compose.runOnIdle { assertEquals(1140, value.value.startMinute); assertEquals(420, value.value.endMinute) }
    }

    @Test fun russianScheduleFitsCompactRows() = settings("ru")
    @Test fun englishScheduleFitsCompactRows() = settings("en")
    @Test fun chineseScheduleFitsCompactRows() = settings("zh-CN")

    @Test fun compactTimeStillOpensNativePickerWithoutChangingTheScheduleOnCancel() {
        val value = mutableStateOf(EventPreferences(startMinute = 1140, endMinute = 420))
        compose.setContent { KMVMSTheme { Column { EventPersonalSettings(value.value, cameras, false) { value.value = it } } } }
        compose.onNodeWithTag("events-hours-from").performClick()
        val instrumentation = androidx.test.platform.app.InstrumentationRegistry.getInstrumentation()
        fun hasPicker(node: android.view.accessibility.AccessibilityNodeInfo?): Boolean {
            if (node == null) return false
            if (node.className?.toString() == "android.widget.TimePicker") return true
            return (0 until node.childCount).any { hasPicker(node.getChild(it)) }
        }
        compose.waitUntil(5_000) { hasPicker(instrumentation.uiAutomation.rootInActiveWindow) }
        instrumentation.sendKeyDownUpSync(android.view.KeyEvent.KEYCODE_BACK)
        compose.waitForIdle()
        compose.runOnIdle { assertEquals(1140, value.value.startMinute); assertEquals(420, value.value.endMinute); assertFalse(value.value.hoursEnabled) }
    }

    @Test fun receivingUsesSavedChoiceAndDisablesUnsupportedProtocol() {
        var changed: Pair<Long, Boolean>? = null
        compose.setContent { KMVMSTheme { EventReceivingScreen(EventsState(cameras = cameras + EventCamera(3, "RTSP only", false, "rtsp"))) { id, enabled -> changed = id to enabled } } }
        compose.onNodeWithTag("events-receiving-camera-1").assertIsOn().performClick()
        compose.runOnIdle { assertEquals(1L to false, changed) }
        compose.onNodeWithTag("events-receiving-camera-3").assertIsNotEnabled()
        compose.onNodeWithTag("events-receiving-camera-2").assertIsOff()
    }

    @Test fun receivingCannotEditAStaleListWhileFreshSettingsAreLoading() {
        compose.setContent { KMVMSTheme { EventReceivingScreen(EventsState(cameras = cameras, settingsLoading = true)) { _, _ -> fail("must wait for fresh settings") } } }
        compose.onNodeWithTag("events-receiving-camera-1").assertIsOn().assertIsNotEnabled()
        compose.onNodeWithTag("events-receiving-camera-2").assertIsOff().assertIsNotEnabled()
    }
}
