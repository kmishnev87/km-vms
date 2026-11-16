package ru.kmvms.mobile.ui.archive

import androidx.compose.foundation.layout.*
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.ui.KMVMSTheme

@RunWith(AndroidJUnit4::class)
class RecordingSwipeCardTest {
    @get:Rule val compose = createComposeRule()
    @Test fun swipeRevealsTrashButOnlyItsClickRequestsConfirmation() {
        var requests = 0
        compose.setContent { KMVMSTheme {
            RecordingSwipeCard("test", true, false, { requests++ }) {
                Surface(Modifier.fillMaxWidth().height(100.dp), color = Color.White) { Text("Recording") }
            }
        } }
        val before = compose.onNodeWithTag("recording-swipe-test").getUnclippedBoundsInRoot()
        compose.onNodeWithTag("recording-delete-test").assertDoesNotExist()
        compose.onNodeWithTag("recording-swipe-test").performTouchInput { swipeLeft() }
        compose.waitForIdle()
        compose.runOnIdle { assertEquals(0, requests) }
        val after = compose.onNodeWithTag("recording-swipe-test").getUnclippedBoundsInRoot()
        assertEquals(before.bottom - before.top, after.bottom - after.top)
        compose.onNodeWithTag("recording-delete-test").assertIsDisplayed().performClick()
        compose.runOnIdle { assertEquals(1, requests) }
        compose.onNodeWithTag("recording-swipe-test").performTouchInput { swipeRight() }
        compose.onNodeWithTag("recording-delete-test").assertDoesNotExist()
    }
    @Test fun noDeletePermissionMeansNoHiddenDestructiveAction() {
        compose.setContent { KMVMSTheme {
            RecordingSwipeCard("readonly", false, false, { error("no permission") }) {
                Surface(Modifier.fillMaxWidth().height(100.dp)) { Text("Recording") }
            }
        } }
        compose.onNodeWithTag("recording-swipe-readonly").performTouchInput { swipeLeft() }
        compose.onNodeWithTag("recording-delete-readonly").assertDoesNotExist()
    }
}
