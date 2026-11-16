package ru.kmvms.mobile.diagnostics

import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.ui.ApkDiagnosticsPanel
import ru.kmvms.mobile.ui.KMVMSTheme

@RunWith(AndroidJUnit4::class)
class ApkReportUiTest {
    @get:Rule val compose = createComposeRule()
    @Test fun reportIsExplicitAndCommentCanBeCancelledWithoutSending() {
        compose.setContent { KMVMSTheme { ApkDiagnosticsPanel() } }
        compose.onNodeWithTag("apk-report-open").performClick()
        compose.onNodeWithTag("apk-report-comment").performTextInput("A typed test comment")
        compose.onNodeWithTag("apk-report-send").assertIsEnabled()
        compose.onNodeWithTag("apk-report-receipt").assertDoesNotExist()
        compose.onNodeWithTag("apk-report-cancel").performClick()
        compose.waitForIdle()
        compose.onNodeWithTag("apk-report-send").assertDoesNotExist()
    }
}
