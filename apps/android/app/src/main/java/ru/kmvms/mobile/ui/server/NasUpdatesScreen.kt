package ru.kmvms.mobile.ui.server

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.background
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.server.*
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.Accent
import ru.kmvms.mobile.ui.Outline
import ru.kmvms.mobile.ui.MeasuredTransfer
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

@Composable fun NasUpdatesScreen(api: NasApi, namespace: String, close: ()->Unit, message:(String)->Unit) {
    val context=LocalContext.current
    val controller=remember(api,namespace){NasUpdatesController(context,NasUpdatesApi(api),namespace)}
    val state by controller.state.collectAsStateWithLifecycle()
    var confirm by remember{mutableStateOf<NasUpdateCandidate?>(null)}
    val lifecycle=LocalLifecycleOwner.current.lifecycle
    val error=stringResource(R.string.server_error)
    BackHandler{close()}
    DisposableEffect(controller,lifecycle){
        controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED))
        val observer=LifecycleEventObserver{_,_->controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED))}
        lifecycle.addObserver(observer)
        onDispose{lifecycle.removeObserver(observer);controller.close()}
    }
    LaunchedEffect(state.failures){if(state.failures>0)message(error)}
    Column(Modifier.fillMaxSize()) {
        Row(verticalAlignment=Alignment.CenterVertically){
            IconButton(onClick=close){Icon(Icons.AutoMirrored.Filled.ArrowBack,stringResource(R.string.back))}
            Text(stringResource(R.string.nas_updates),style=MaterialTheme.typography.titleLarge)
        }
        Box(Modifier.height(6.dp).fillMaxWidth()){if(state.loading||state.posting)LinearProgressIndicator(Modifier.fillMaxWidth())}
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(16.dp),verticalArrangement=Arrangement.spacedBy(14.dp)){
            Panel {
                Text(stringResource(R.string.nas_installed_version,state.check?.installed?:"—"),style=MaterialTheme.typography.titleMedium)
                Text(stringResource(when{state.checking->R.string.nas_checking;state.check?.candidate!=null->R.string.nas_update_available;state.check?.status=="current"->R.string.nas_current;else->R.string.nas_update_check_needed}),color=Muted)
                state.check?.candidate?.let{Text(it.version,style=MaterialTheme.typography.titleLarge)}
                Row(horizontalArrangement=Arrangement.spacedBy(8.dp)){
                    OutlinedButton(onClick={controller.refresh(true)},enabled=!state.loading&&!state.posting&&state.pending==null,modifier=Modifier.weight(1f).testTag("nas-update-check")){Text(stringResource(R.string.mobile_update_check))}
                    Button(onClick={confirm=state.check?.candidate},enabled=state.check?.candidate!=null&&state.pending==null&&!state.posting&&!state.loading,modifier=Modifier.weight(1f).testTag("nas-update-apply")){Text(stringResource(R.string.nas_update))}
                }
            }
            if(state.progress?.visible==true||state.pending!=null)
                NasUpdateOperation(state,controller::refreshStatus,controller::retry,controller::cancel)
        }
    }
    confirm?.let{candidate->AlertDialog(onDismissRequest={confirm=null},title={Text(stringResource(R.string.nas_updates))},
        text={Text(stringResource(R.string.nas_update_confirmation,candidate.version))},
        confirmButton={TextButton(onClick={confirm=null;controller.start(candidate)},modifier=Modifier.testTag("confirm-nas-update")){Text(stringResource(R.string.nas_update))}},
        dismissButton={TextButton(onClick={confirm=null}){Text(stringResource(R.string.cancel))}})}
}

@Composable internal fun NasUpdateOperation(state: NasUpdatesState, checkStatus: () -> Unit, retry: () -> Unit, cancel: () -> Unit) {
    val progress = state.progress
    val success = progress?.completed(state.pending) == true
    val failed = progress?.terminal == true && progress.status != "completed"
    val completeColor = Color(0xFF15803D)
    Panel {
        Text(stringResource(when {
            success -> R.string.operation_completed
            progress?.status == "completed" -> R.string.operation_unknown
            progress?.status == "failed_rolled_back" -> R.string.nas_rolled_back
            failed -> R.string.operation_failed
            state.reconnecting -> R.string.nas_reconnecting
            progress?.visible == true || state.posting -> R.string.operation_running
            else -> R.string.operation_unknown
        }), color = if(success) completeColor else if(failed) MaterialTheme.colorScheme.error else Muted,
            modifier = Modifier.testTag("nas-update-result"))
        (progress?.targetVersion ?: state.pending?.candidate?.version)?.let {
            Text(stringResource(if (success) R.string.nas_installed_version else R.string.nas_target_version, it),
                style = MaterialTheme.typography.titleMedium)
        }
        val reportedSteps = progress?.steps.orEmpty().associateBy { it.name }
        if (reportedSteps.isNotEmpty()) {
            val active = nasUpdateStepOrder.indexOfFirst { reportedSteps[it]?.status in setOf("running", "failed") }
                .takeIf { it >= 0 }
                ?: nasUpdateStepOrder.indexOf(progress?.step).takeIf { it >= 0 }
                ?: nasUpdateStepOrder.indexOfFirst { reportedSteps[it]?.status != "completed" }.takeIf { it >= 0 }
                ?: nasUpdateStepOrder.lastIndex
            Text(stringResource(R.string.nas_step_count, active + 1, nasUpdateStepOrder.size,
                stringResource(stepLabel(nasUpdateStepOrder[active]))),
                style = MaterialTheme.typography.titleSmall, modifier = Modifier.testTag("nas-update-stage"))
            Row(Modifier.fillMaxWidth().testTag("nas-update-steps"), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                nasUpdateStepOrder.forEach { name ->
                    val status = reportedSteps[name]?.status
                    val color = when {
                        success -> completeColor
                        status == "failed" -> MaterialTheme.colorScheme.error
                        status == "completed" -> Accent
                        status == "running" -> Accent.copy(alpha = 0.45f)
                        else -> Outline
                    }
                    Box(Modifier.weight(1f).height(7.dp).clip(RoundedCornerShape(4.dp)).background(color)
                        .testTag("nas-step-$name-${if(success) "completed" else status ?: "pending"}"))
                }
            }
        }
        if (!success) Text(stringResource(phaseLabel(progress)), style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier.testTag("nas-update-detail"))
        if (progress?.percent != null && progress.current != null && progress.total != null) {
            if (progress.unit == "bytes") MeasuredTransfer(progress.current, progress.total, "nas-transfer")
            else {
                Text(stringResource(R.string.nas_items_progress, progress.current, progress.total, progress.percent),
                    style = MaterialTheme.typography.bodySmall)
                LinearProgressIndicator(progress={progress.percent / 100f}, modifier=Modifier.fillMaxWidth())
            }
        }
        progress?.elapsed?.let { Text(stringResource(R.string.nas_elapsed, it / 60, it % 60),
            color=Muted, style=MaterialTheme.typography.bodySmall) }
        if (state.reconnecting || progress?.status == "completed" && !success)
            Text(stringResource(R.string.operation_unknown), color=Muted, style=MaterialTheme.typography.bodySmall)
        Row(horizontalArrangement=Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick=checkStatus, enabled=!state.loading&&!state.posting,
                modifier=Modifier.weight(1f).testTag("nas-update-status-check")) {
                Text(stringResource(if(state.statusRequested) R.string.nas_status_checking else R.string.check_status))
            }
            if(state.pending!=null&&!state.watching) OutlinedButton(onClick=retry, enabled=!state.posting,
                modifier=Modifier.weight(1f)) { Text(stringResource(R.string.retry)) }
            if(progress?.canCancel==true) OutlinedButton(onClick=cancel, enabled=!state.posting,
                modifier=Modifier.weight(1f)) { Text(stringResource(R.string.cancel)) }
        }
        Box(Modifier.fillMaxWidth().heightIn(min=40.dp)) {
            if (state.statusCheckFailed) Text(stringResource(R.string.nas_status_failed),
                style=MaterialTheme.typography.bodySmall, color=MaterialTheme.colorScheme.error,
                modifier=Modifier.testTag("nas-update-receipt"))
            else state.statusCheckedAt?.let {
                val time = DateTimeFormatter.ofPattern("HH:mm:ss").withZone(ZoneId.systemDefault()).format(Instant.ofEpochMilli(it))
                Text(stringResource(R.string.nas_status_received,time), style=MaterialTheme.typography.bodySmall,
                    color=Muted, modifier=Modifier.testTag("nas-update-receipt"))
            }
        }
    }
}

private fun stepLabel(name: String?) = when(name) {
    "request" -> R.string.nas_step_request
    "preflight" -> R.string.nas_step_preflight
    "applying" -> R.string.nas_step_applying
    "health_check" -> R.string.nas_step_health
    "commit_verification" -> R.string.nas_step_verification
    else -> R.string.operation_unknown
}

private fun phaseLabel(progress: NasUpdateProgress?): Int = when(progress?.phase ?: progress?.step) {
    "queued", "starting_helper" -> R.string.nas_step_request
    "acquire_source", "downloading" -> R.string.nas_phase_download
    "extracting", "staging_source", "target_prepared" -> R.string.nas_phase_source
    "validating_source", "validating_compose", "compose_config", "verifying_images" -> R.string.nas_phase_validation
    "building_images", "rebuilding", "rebuild_recreate" -> R.string.nas_phase_build
    "pulling_images" -> R.string.nas_phase_pull
    "schema_preflight", "schema_preparing" -> R.string.nas_phase_database
    "overlay", "applying", "activating", "restarting", "reconnecting", "activating_target" -> R.string.nas_step_applying
    "health_check", "verifying_target" -> R.string.nas_step_health
    "commit_verification", "completed" -> R.string.nas_step_verification
    "rolling_back", "failed_rolled_back" -> R.string.nas_phase_rollback
    else -> stepLabel(progress?.step)
}
