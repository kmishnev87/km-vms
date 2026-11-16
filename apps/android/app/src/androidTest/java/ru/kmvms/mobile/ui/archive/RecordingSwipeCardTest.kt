package ru.kmvms.mobile.ui.archive

import androidx.compose.foundation.layout.*
import androidx.compose.runtime.*
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
            var revealed by remember { mutableStateOf(false) }
            RecordingSwipeCard("test", true, false, revealed, { revealed=it }, { requests++ }) {
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
            RecordingSwipeCard("readonly", false, false, false, { error("no permission") }, { error("no permission") }) {
                Surface(Modifier.fillMaxWidth().height(100.dp)) { Text("Recording") }
            }
        } }
        compose.onNodeWithTag("recording-swipe-readonly").performTouchInput { swipeLeft() }
        compose.onNodeWithTag("recording-delete-readonly").assertDoesNotExist()
    }

    @Test fun multipleRevealedRowsShowOneAlignedBulkActionAndSwipingBackRemovesIt() {
        var requests=0
        var selected=emptySet<Int>()
        compose.setContent { KMVMSTheme {
            var revealed by remember { mutableStateOf(emptySet<Int>()) }
            selected=revealed
            Column(Modifier.fillMaxWidth()) {
                RecordingsSelectionBar(3,"UTC",revealed.size,false,true,false,{requests++})
                Column(Modifier.fillMaxWidth().padding(horizontal=16.dp)) {
                    (1..3).forEach { id ->
                        RecordingSwipeCard("$id",true,false,id in revealed,
                            { open -> revealed=if(open)revealed+id else revealed-id },{requests++}) {
                            Surface(Modifier.fillMaxWidth().height(100.dp),color=Color.White) { Text("Recording $id") }
                        }
                    }
                }
            }
        } }
        compose.onNodeWithTag("recordings-delete-selected").assertDoesNotExist()
        compose.onNodeWithTag("recording-swipe-1").performTouchInput { swipeLeft() }
        compose.onNodeWithTag("recordings-delete-selected").assertDoesNotExist()
        compose.onNodeWithTag("recording-swipe-2").performTouchInput { swipeLeft() }
        compose.onNodeWithTag("recordings-delete-selected").assertIsDisplayed()
        val bulk=compose.onNodeWithTag("recordings-delete-selected").getUnclippedBoundsInRoot()
        for(id in 1..2) {
            val row=compose.onNodeWithTag("recording-delete-$id").getUnclippedBoundsInRoot()
            assertEquals((bulk.left.value+bulk.right.value)/2,(row.left.value+row.right.value)/2,.5f)
        }
        compose.runOnIdle { assertEquals(0,requests);assertEquals(setOf(1,2),selected) }
        compose.onNodeWithTag("recordings-delete-selected").performClick()
        compose.runOnIdle { assertEquals(1,requests) }
        compose.onNodeWithTag("recording-swipe-1").performTouchInput { swipeRight() }
        compose.onNodeWithTag("recordings-delete-selected").assertDoesNotExist()
        compose.onNodeWithTag("recording-delete-2").assertIsDisplayed()
    }

    @Test fun aReadOnlyUserNeverGetsTheBulkAction() {
        compose.setContent { KMVMSTheme { RecordingsSelectionBar(3,"UTC",3,false,false,false,{error("no permission")}) } }
        compose.onNodeWithTag("recordings-delete-selected").assertDoesNotExist()
    }
}
