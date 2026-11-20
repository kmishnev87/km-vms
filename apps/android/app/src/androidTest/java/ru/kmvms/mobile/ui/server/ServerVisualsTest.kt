package ru.kmvms.mobile.ui.server

import androidx.compose.foundation.layout.Column
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.data.NasUser
import ru.kmvms.mobile.server.*
import ru.kmvms.mobile.ui.KMVMSTheme

@RunWith(AndroidJUnit4::class)
class ServerVisualsTest {
    @get:Rule val compose=createComposeRule()
    @Test fun systemStatesHaveDistinctHonestColorsAndReadableLabels() {
        assertEquals(0xFF278554.toInt(),severityColor("ok").toArgb())
        assertEquals(4,listOf("ok","warning","error","unknown").map{severityColor(it)}.toSet().size)
        compose.setContent { KMVMSTheme { Column {
            listOf("ok","warning","error","unknown").forEach{SystemStatusIndicator(it,androidx.compose.ui.Modifier.testTag("state-$it"))}
        } } }
        listOf("ok","warning","error","unknown").forEach{compose.onNodeWithTag("state-$it").assertIsDisplayed()}
    }
    @Test fun archiveStatusIsCenteredAgainstTheWholeArchiveRootContent() {
        compose.setContent { KMVMSTheme { ArchiveRootCard(ArchiveRoot("test","Archive folder","/storage/archive/cameras",false,true,1024,false,recordingAccess="ready")) } }
        val row=compose.onNodeWithTag("archive-root-test").getUnclippedBoundsInRoot()
        val label=compose.onNodeWithTag("archive-root-status-test").getUnclippedBoundsInRoot()
        assertEquals(((row.top+row.bottom)/2).value,((label.top+label.bottom)/2).value,.5f)
    }
    @Test fun createUserRequiresMatchingPasswordAndDoesNotOfferAdministratorToAnAdministrator() {
        val actor=NasUser(2,"Admin","admin",setOf("manage_users"))
        compose.setContent { KMVMSTheme { UserEditor(actor,null,false,null,{}, {}) } }
        compose.onNodeWithTag("user-save").assertIsNotEnabled()
        compose.onNodeWithTag("user-username").performTextInput("ui-fixture")
        compose.onNodeWithTag("user-password").performScrollTo().performTextInput("fixture-pass-123")
        compose.onNodeWithTag("user-confirm-password").performScrollTo().performTextInput("different-pass")
        compose.onNodeWithTag("user-save").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithTag("user-confirm-password").performScrollTo().performTextReplacement("fixture-pass-123")
        compose.onNodeWithTag("user-save").performScrollTo().assertIsEnabled()
        compose.onNodeWithTag("user-role").performScrollTo().performClick()
        compose.onAllNodesWithText("Administrator").assertCountEquals(0)
        compose.onAllNodesWithText("Owner").assertCountEquals(0)
    }
    @Test fun ownAccountCannotBeDisabledAndPasswordChangeNeedsCurrentPassword() {
        val actor=NasUser(1,"Owner","owner",setOf("manage_users"))
        val target=ManagedUser(1,"owner","Owner","owner",true)
        compose.setContent { KMVMSTheme { UserEditor(actor,target,false,null,{}, {}) } }
        compose.onNodeWithTag("user-active").assertIsNotEnabled()
        compose.onNodeWithTag("user-password").performScrollTo().performTextInput("fixture-pass-123")
        compose.onNodeWithTag("user-confirm-password").performScrollTo().performTextInput("fixture-pass-123")
        compose.onNodeWithTag("user-save").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithTag("user-current-password").performScrollTo().performTextInput("old-fixture-pass")
        compose.onNodeWithTag("user-save").performScrollTo().assertIsEnabled()
    }
}
