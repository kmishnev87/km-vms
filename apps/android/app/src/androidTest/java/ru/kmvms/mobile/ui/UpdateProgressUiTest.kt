package ru.kmvms.mobile.ui

import android.graphics.Bitmap
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.semantics.ProgressBarRangeInfo
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.R
import ru.kmvms.mobile.server.*
import ru.kmvms.mobile.ui.server.NasUpdateOperation
import ru.kmvms.mobile.update.*
import java.io.File

@RunWith(AndroidJUnit4::class)
class UpdateProgressUiTest {
    @get:Rule val compose=createComposeRule()
    private val context get()=InstrumentationRegistry.getInstrumentation().targetContext
    private val release=AndroidRelease("0.8.3",803,"v0.9.19",1_000_000,"ab".repeat(32),"cd".repeat(32),26)

    @Test fun downloadingShowsRealBytesAndAChangingFilledBar() {
        var bytes by mutableLongStateOf(250_000)
        compose.setContent { KMVMSTheme {
            Column(Modifier.fillMaxSize().background(PageBackground).padding(16.dp)) {
                AppUpdateStatus(AppUpdateState(UpdatePhase.DOWNLOADING,release,bytes))
            }
        } }
        compose.onNodeWithTag("apk-transfer-percent").assertTextEquals("25%")
        compose.onNodeWithTag("apk-transfer-bytes").assertIsDisplayed()
        compose.onNodeWithTag("apk-transfer-bar").assertRangeInfoEquals(ProgressBarRangeInfo(0.25f,0f..1f))
        capture("apk-download-25")
        compose.runOnIdle { bytes=980_000 }
        compose.onNodeWithTag("apk-transfer-percent").assertTextEquals("98%")
        compose.onNodeWithTag("apk-transfer-bar").assertRangeInfoEquals(ProgressBarRangeInfo(0.98f,0f..1f))
        capture("apk-download-98")
    }

    @Test fun completedDownloadShowsVerificationNotAnUnfinishedTransfer() {
        compose.setContent { KMVMSTheme { AppUpdateStatus(AppUpdateState(UpdatePhase.VERIFYING,release,1_000_000)) } }
        compose.onNodeWithTag("apk-update-phase").assertTextEquals(context.getString(R.string.mobile_update_verifying))
        compose.onNodeWithTag("apk-transfer-percent").assertTextEquals("100%")
    }

    @Test fun anAcceptedAutomaticOfferKeepsItsProgressVisibleAndCanBeHidden() {
        var hidden=0
        compose.setContent { KMVMSTheme {
            AppUpdateOfferContent(AppUpdateState(UpdatePhase.DOWNLOADING,release,250_000,offer=true),true,{}, {hidden++})
        } }
        compose.onNodeWithTag("apk-transfer-percent").assertIsDisplayed().assertTextEquals("25%")
        compose.onNodeWithText(context.getString(R.string.mobile_update_download)).assertDoesNotExist()
        compose.onNodeWithText(context.getString(R.string.mobile_update_hide)).performClick()
        compose.runOnIdle { assertEquals(1,hidden) }
    }

    @Test fun nasShowsTheServerStageAndDetailsAndAcknowledgesTheStatusButton() {
        var state by mutableStateOf(NasUpdatesState(progress=progress("staging","building_images"),watching=true))
        var checks=0
        compose.setContent { KMVMSTheme {
            Column(Modifier.fillMaxSize().background(PageBackground).padding(16.dp)) {
                NasUpdateOperation(state,{checks++;state=state.copy(statusCheckedAt=1_700_000_000_000)}, {}, {})
            }
        } }
        compose.onNodeWithTag("nas-update-stage").assertTextEquals(context.getString(R.string.nas_step_count,2,5,context.getString(R.string.nas_step_preflight)))
        compose.onNodeWithTag("nas-update-detail").assertTextEquals(context.getString(R.string.nas_phase_build))
        compose.onNodeWithTag("nas-transfer-bar").assertDoesNotExist()
        compose.onNodeWithTag("nas-update-status-check").performClick()
        compose.onNodeWithTag("nas-update-receipt").assertIsDisplayed()
        compose.runOnIdle { assertEquals(1,checks) }
        capture("nas-building-components")
    }

    @Test fun onlyAVerifiedInstalledCommitShowsTheSuccessfulFinalResult() {
        var verified by mutableStateOf(true)
        compose.setContent { KMVMSTheme {
            Column(Modifier.fillMaxSize().background(PageBackground).padding(16.dp)) {
                NasUpdateOperation(NasUpdatesState(progress=progress("completed","completed",verified)),{}, {}, {})
            }
        } }
        compose.onNodeWithTag("nas-update-result").assertTextEquals(context.getString(R.string.operation_completed))
        nasUpdateStepOrder.forEach { compose.onNodeWithTag("nas-step-$it-completed").assertIsDisplayed() }
        capture("nas-verified-completion")
        compose.runOnIdle { verified=false }
        compose.onNodeWithTag("nas-update-result").assertTextEquals(context.getString(R.string.operation_unknown))
    }

    @Test fun aQueuedOperationStartsAtTheFirstStepNotAtVerification() {
        val queued=NasUpdateProgress(null,"queued",null,0,null,false,null,true,
            steps=nasUpdateStepOrder.map{NasUpdateStep(it,"pending")})
        compose.setContent { KMVMSTheme { NasUpdateOperation(NasUpdatesState(progress=queued),{}, {}, {}) } }
        compose.onNodeWithTag("nas-update-stage").assertTextEquals(context.getString(R.string.nas_step_count,1,5,
            context.getString(R.string.nas_step_request)))
    }

    private fun progress(status:String,phase:String,verified:Boolean=false):NasUpdateProgress {
        val payload=JSONObject().put("status",status).put("phase",phase).put("current_step",if(status=="completed")"commit_verification"else"preflight")
            .put("target_version","0.9.18").put("elapsed_seconds",281).put("commit_verified",verified).put("installed_commit","a".repeat(40))
        payload.put("steps",org.json.JSONArray(nasUpdateStepOrder.mapIndexed{index,name->JSONObject().put("name",name)
            .put("status",if(status=="completed"||index==0)"completed"else if(index==1)"running"else"pending")}))
        return NasUpdateProgress.from(payload)
    }

    private fun capture(name:String) {
        if(InstrumentationRegistry.getArguments().getString("captureUpdateUx")!="true")return
        val directory=File(context.getExternalFilesDir(null),"update-ux-20261115").apply{mkdirs()}
        val bitmap=compose.onRoot().captureToImage().asAndroidBitmap()
        File(directory,"$name.png").outputStream().use{bitmap.compress(Bitmap.CompressFormat.PNG,100,it)}
    }
}
