package ru.kmvms.mobile.ui.events

import androidx.compose.runtime.*
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.push.PushStatus
import ru.kmvms.mobile.ui.KMVMSTheme
import ru.kmvms.mobile.R
import androidx.test.platform.app.InstrumentationRegistry

/** UI component tests with explicit registration results, not a provider-delivery claim. */
@RunWith(AndroidJUnit4::class)
class PushSetupDialogTest {
    @get:Rule val compose = createComposeRule()

    @Test fun firstOfferNamesTheServerAndDeclineDoesNotStartRegistration() {
        var enabled = 0
        var closed = false
        compose.setContent { KMVMSTheme {
            PushSetupDialog("Owned test NAS", false, state(PushStatus.OFF), { enabled++ }, { closed = true })
        } }
        compose.onNodeWithText("Owned test NAS", substring = true).assertIsDisplayed()
        compose.onNodeWithTag("push-setup-accept").assertIsEnabled()
        compose.onNodeWithTag("push-setup-decline").performClick()
        compose.runOnIdle { assertTrue(closed); assertEquals(0, enabled) }
    }

    @Test fun acceptShowsRegistrationThenActualReadyAndTheTestAction() {
        var accepted by mutableStateOf(false)
        var status by mutableStateOf(PushStatus.REGISTERING)
        var tests = 0
        compose.setContent { KMVMSTheme {
            PushSetupDialog("Owned NAS", accepted, state(status, test = { tests++ }), { accepted = true }, {})
        } }
        compose.onNodeWithTag("push-setup-accept").performClick()
        compose.onNodeWithTag("push-setup-status").assertIsDisplayed()
        compose.onNodeWithTag("push-setup-test").assertDoesNotExist()
        compose.runOnIdle { status = PushStatus.READY }
        compose.onNodeWithTag("push-setup-test").assertIsEnabled().performClick()
        compose.runOnIdle { assertEquals(1, tests) }
    }

    @Test fun grantedPermissionDoesNotTurnARegistrationFailureIntoSuccess() {
        var retries = 0
        compose.setContent { KMVMSTheme {
            PushSetupDialog("Owned NAS", true, state(PushStatus.FAILED, enable = { retries++ }), {}, {})
        } }
        compose.onNodeWithTag("push-setup-test").assertDoesNotExist()
        compose.onNodeWithTag("push-setup-retry").performClick()
        compose.runOnIdle { assertEquals(1, retries) }
    }

    @Test fun aTestDeliveryFailureIsVisibleInsideTheDialogEvenWithARegisteredEndpoint() {
        val failure = InstrumentationRegistry.getInstrumentation().targetContext.getString(R.string.push_failed)
        compose.setContent { KMVMSTheme {
            PushSetupDialog("Owned NAS", true, state(PushStatus.READY, feedback = R.string.push_failed), {}, {})
        } }
        compose.onNodeWithTag("push-setup-status").assertTextEquals(failure)
        compose.onNodeWithTag("push-setup-test").assertIsEnabled()
    }

    private fun state(status: PushStatus, enable: () -> Unit = {}, test: () -> Unit = {}, feedback: Int? = null) =
        PushRegistrationUi(null, true, false, status, enable, {}, {}, test, feedback)
}
