package ru.kmvms.mobile.ui.server

import androidx.compose.foundation.layout.Column
import androidx.compose.runtime.*
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.R
import ru.kmvms.mobile.ui.KMVMSTheme
import ru.kmvms.mobile.ui.camera.LabeledChoice
import ru.kmvms.mobile.ui.live.LiveCameraCaption

@RunWith(AndroidJUnit4::class)
class OnlineModeUiTest {
    @get:Rule val compose=createComposeRule()
    @Test fun theModePickerUsesTheFormatButtonHeightAndFastComesFirst() {
        var value by mutableStateOf("stable")
        compose.setContent { KMVMSTheme { Column {
            LabeledChoice("Format","MKV",listOf("mkv" to "MKV"),true,"format",{})
            OnlineModeChoice(value,true){value=it}
        } } }
        val format=compose.onNodeWithTag("format").getUnclippedBoundsInRoot()
        val online=compose.onNodeWithTag("nas-online-mode").getUnclippedBoundsInRoot()
        assertEquals((format.bottom-format.top).value,(online.bottom-online.top).value,.5f)
        compose.onNodeWithTag("nas-online-mode").performClick()
        val context=InstrumentationRegistry.getInstrumentation().targetContext
        val fast=compose.onNodeWithText(context.getString(R.string.online_mode_fast))
        val stable=compose.onAllNodesWithText(context.getString(R.string.online_mode_stable)).onLast()
        assertTrue(fast.getUnclippedBoundsInRoot().top<stable.getUnclippedBoundsInRoot().top)
        fast.performClick(); compose.runOnIdle { assertEquals("fast",value) }
    }
    @Test fun disabledModeCannotBeOpenedOrChanged() {
        compose.setContent { KMVMSTheme { OnlineModeChoice("stable",false) { error("unauthorized choice") } } }
        compose.onNodeWithTag("nas-online-mode").assertIsNotEnabled()
    }
    @Test fun selectedModeHasAConciseCaptionAndHelpSeparatesBothModes() {
        var value by mutableStateOf("stable")
        val context=InstrumentationRegistry.getInstrumentation().targetContext
        compose.setContent { KMVMSTheme { OnlineModeChoice(value,true){value=it} } }
        compose.onNodeWithTag("nas-online-mode-summary")
            .assertTextEquals(context.getString(R.string.online_mode_stable_summary))
        compose.onNodeWithTag("nas-online-mode").performClick()
        compose.onNodeWithText(context.getString(R.string.online_mode_fast)).performClick()
        compose.onNodeWithTag("nas-online-mode-summary")
            .assertTextEquals(context.getString(R.string.online_mode_fast_summary))
        val help=context.getString(R.string.online_mode_help)
        val paragraphs=help.split("\n\n")
        assertEquals(2,paragraphs.size)
        assertTrue(paragraphs[0].startsWith(context.getString(R.string.online_mode_fast)))
        assertTrue(paragraphs[1].startsWith(context.getString(R.string.online_mode_stable)))
        compose.onNodeWithTag("nas-online-mode-help").performClick()
        compose.onNodeWithText(help).assertIsDisplayed()
    }
    @Test fun selectedModeHasAShortCaptionAndHelpHasTwoParagraphs() {
        val context=InstrumentationRegistry.getInstrumentation().targetContext
        var value by mutableStateOf("stable")
        compose.setContent { KMVMSTheme { OnlineModeChoice(value,true) { value=it } } }
        compose.onNodeWithTag("nas-online-mode-summary")
            .assertTextEquals(context.getString(R.string.online_mode_stable_summary))
        compose.onNodeWithTag("nas-online-mode").performClick()
        compose.onNodeWithText(context.getString(R.string.online_mode_fast)).performClick()
        compose.onNodeWithTag("nas-online-mode-summary")
            .assertTextEquals(context.getString(R.string.online_mode_fast_summary))
        compose.onNodeWithTag("nas-online-mode-help").performClick()
        val help=context.getString(R.string.online_mode_help)
        val paragraphs=help.split("\n\n")
        assertEquals(2,paragraphs.size)
        assertTrue(paragraphs[0].startsWith(context.getString(R.string.online_mode_fast)))
        assertTrue(paragraphs[1].startsWith(context.getString(R.string.online_mode_stable)))
        compose.onNodeWithText(help).assertIsDisplayed()
    }
    @Test fun fallbackIsASeparateTwoLineCaptionNotAPlaybackError() {
        compose.setContent { KMVMSTheme { LiveCameraCaption("Garden",true) } }
        compose.onNodeWithText("Garden").assertIsDisplayed()
        val node=compose.onNodeWithTag("live-hls-fallback").assertIsDisplayed()
        val text=node.fetchSemanticsNode().config[androidx.compose.ui.semantics.SemanticsProperties.Text].joinToString { it.text }
        assertTrue(text.contains("\n"));assertTrue(text.contains("HLS"))
    }
}
