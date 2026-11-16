package ru.kmvms.mobile.diagnostics

import android.os.Bundle
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assume.assumeTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.ui.ApkDiagnosticsPanel
import ru.kmvms.mobile.ui.KMVMSTheme

/** Explicit opt-in only: sends one engineering report to the real support inbox. */
@RunWith(AndroidJUnit4::class)
class ApkSupportEndToEndTest {
    @get:Rule val compose = createComposeRule()
    @Test fun sendViaAppUiGetsReceiptFromRealPublicReceiver() {
        assumeTrue(InstrumentationRegistry.getArguments().getString("allow_report_submission") == "true")
        compose.setContent { KMVMSTheme { ApkDiagnosticsPanel() } }
        compose.onNodeWithTag("apk-report-open").performClick()
        compose.onNodeWithTag("apk-report-comment").performTextInput("Engineering validation 2026-11-16: Android app report, direct JSON delivery, no phone ZIP.")
        compose.onNodeWithTag("apk-report-send").performClick()
        compose.waitUntil(110000) { compose.onAllNodesWithTag("apk-report-receipt").fetchSemanticsNodes().isNotEmpty() }
        val text = compose.onNodeWithTag("apk-report-receipt").fetchSemanticsNode().config[androidx.compose.ui.semantics.SemanticsProperties.Text].joinToString("") { it.text }
        InstrumentationRegistry.getInstrumentation().sendStatus(0, Bundle().apply { putString("report_id", text) })
        compose.onNodeWithTag("apk-report-done").performClick()
    }
}
