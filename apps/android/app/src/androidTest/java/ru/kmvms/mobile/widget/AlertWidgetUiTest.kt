package ru.kmvms.mobile.widget

import android.content.res.Configuration
import androidx.compose.foundation.layout.*
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
class AlertWidgetUiTest {
    @get:Rule val compose = createComposeRule()
    private fun initial(count: Int = 3) = AlertWidgetState(loading = false, snapshot = AlertWidgetSnapshot(
        EventPreferences(hoursEnabled = true, startMinute = 1140, endMinute = 420),
        (1..count).map { EventCamera(it.toLong(), "Camera $it / long name", true) }, "GMT+05:00"))
    private fun compact(locale: String) {
        var changed: Pair<Long, Boolean>? = null
        compose.setContent {
            val config = Configuration(LocalConfiguration.current).apply { setLocale(Locale.forLanguageTag(locale)) }
            val context = LocalContext.current.createConfigurationContext(config)
            CompositionLocalProvider(LocalContext provides context, LocalConfiguration provides config) { KMVMSTheme {
                Box(Modifier.width(320.dp).height(600.dp)) { AlertWidgetPopup("Home", initial(), true, {}, {}, { id, value -> changed = id to value }, {}) }
            } }
        }
        compose.onNodeWithTag("widget-camera-1").assertIsDisplayed().assertIsOn().performClick()
        compose.runOnIdle { assertEquals(1L to false, changed) }
        val popup = compose.onNodeWithTag("widget-popup").getUnclippedBoundsInRoot()
        val row = compose.onNodeWithTag("widget-camera-1").getUnclippedBoundsInRoot()
        assertTrue(row.left >= popup.left && row.right <= popup.right)
        assertTrue(compose.onAllNodesWithText("19:00", substring = true).fetchSemanticsNodes().isNotEmpty())
    }
    @Test fun russianCompactPopup() = compact("ru")
    @Test fun englishCompactPopup() = compact("en")
    @Test fun chineseCompactPopup() = compact("zh-CN")
    @Test fun manyCamerasScrollWithoutMovingTheMasterControl() {
        compose.setContent { KMVMSTheme { AlertWidgetPopup("Home", initial(80), true, {}, {}, { _, _ -> }, {}) } }
        val master = compose.onNodeWithTag("widget-master").getUnclippedBoundsInRoot()
        compose.onNodeWithTag("widget-cameras").performScrollToNode(hasTestTag("widget-camera-80"))
        compose.onNodeWithTag("widget-camera-80").assertIsDisplayed()
        assertEquals(master, compose.onNodeWithTag("widget-master").getUnclippedBoundsInRoot())
    }
    @Test fun pendingChoiceAndMasterOffCannotAllowMisleadingCameraEdits() {
        val value = mutableStateOf(initial().copy(busy = true))
        compose.setContent { KMVMSTheme { AlertWidgetPopup("Home", value.value, false, {}, { fail("busy") }, { _, _ -> fail("disabled") }, {}) } }
        compose.onNodeWithTag("widget-master").assertIsNotEnabled()
        compose.onNodeWithTag("widget-camera-1").assertIsOn().assertIsNotEnabled()
        compose.onNodeWithTag("widget-phone-warning").assertIsDisplayed()
        compose.runOnIdle { value.value = initial().let { it.copy(snapshot = it.snapshot!!.copy(preferences = it.snapshot.preferences.copy(notifications = false))) } }
        compose.onNodeWithTag("widget-master").assertIsOff().assertIsEnabled()
        compose.onNodeWithTag("widget-camera-1").assertIsOn().assertIsNotEnabled()
    }
}
