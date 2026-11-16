package ru.kmvms.mobile.ui.live

import androidx.compose.foundation.layout.Row
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.ptz.PtzPreset
import ru.kmvms.mobile.ui.KMVMSTheme

@RunWith(AndroidJUnit4::class)
class PtzPresetPickerTest {
    @get:Rule val compose = createComposeRule()
    @Test fun compactDiskOpensPresetsAndChoosesOnceWithoutAnExtraRow() {
        val selected = mutableListOf<String>()
        compose.setContent { KMVMSTheme { Row {
            PresetPicker(listOf(PtzPreset("road-token", "Road"), PtzPreset("home-token", "Home")), true, Color.White, true) { selected.add(it) }
        } } }
        val bounds = compose.onNodeWithTag("ptz-preset-picker").getUnclippedBoundsInRoot()
        assertEquals(40f, (bounds.bottom - bounds.top).value, .1f)
        compose.onNodeWithTag("ptz-presets").assertDoesNotExist()
        compose.onNodeWithTag("ptz-preset-picker").performClick()
        compose.onNodeWithText("Home").performClick()
        compose.onNodeWithTag("ptz-preset-menu").assertDoesNotExist()
        compose.runOnIdle { assertEquals(listOf("home-token"), selected) }
    }
    @Test fun normalPresetButtonRemainsAndUnavailableCommandIsDisabled() {
        compose.setContent { KMVMSTheme {
            PresetPicker(listOf(PtzPreset("road", "Road")), false, Color.Black, false) { error("must not send while unavailable") }
        } }
        compose.onNodeWithTag("ptz-presets").assertIsNotEnabled()
        compose.onNodeWithTag("ptz-preset-picker").assertDoesNotExist()
    }
}
