package ru.kmvms.mobile.ui

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import ru.kmvms.mobile.BuildConfig
import ru.kmvms.mobile.R
import ru.kmvms.mobile.update.*

@Composable fun AppUpdatePanel(updates: AppUpdates) {
    val state by updates.state.collectAsState()
    val occupied = state.phase in setOf(UpdatePhase.CHECKING, UpdatePhase.DOWNLOADING, UpdatePhase.VERIFYING)
    Surface(shape = androidx.compose.foundation.shape.RoundedCornerShape(14.dp), color = androidx.compose.ui.graphics.Color.White) {
        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(stringResource(R.string.mobile_update_title), style = MaterialTheme.typography.titleMedium)
            Text(stringResource(R.string.version) + " " + BuildConfig.VERSION_NAME, color = Muted)
            // A stable slot: checking, progress, and outcomes never move the buttons.
            AppUpdateStatus(state)
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = { updates.check(manual = true) }, enabled = !occupied && state.phase != UpdatePhase.PERMISSION,
                    modifier = Modifier.weight(1f).heightIn(min = 48.dp).testTag("apk-check-update")) { Text(stringResource(R.string.mobile_update_check)) }
                Button(onClick = { if (state.phase in setOf(UpdatePhase.READY, UpdatePhase.PERMISSION)) updates.install() else updates.download() },
                    enabled = state.phase in setOf(UpdatePhase.AVAILABLE, UpdatePhase.READY, UpdatePhase.PERMISSION) ||
                        (state.phase == UpdatePhase.FAILED && state.release != null && !BuildConfig.DEBUG && state.release!!.code > BuildConfig.VERSION_CODE),
                    modifier = Modifier.weight(1f).heightIn(min = 48.dp).testTag("apk-install-update")) {
                    Text(stringResource(if (state.phase in setOf(UpdatePhase.READY, UpdatePhase.PERMISSION)) R.string.mobile_update_install else R.string.mobile_update_download))
                }
            }
        }
    }
}

@Composable fun AppUpdateOffer(updates: AppUpdates, visible: Boolean) {
    val state by updates.state.collectAsState()
    AppUpdateOfferContent(state, visible, updates::download, updates::dismiss)
}

@Composable internal fun AppUpdateOfferContent(state: AppUpdateState, visible: Boolean, download: () -> Unit, dismiss: () -> Unit) {
    val transferring = state.phase in setOf(UpdatePhase.DOWNLOADING, UpdatePhase.VERIFYING)
    if (visible && state.offer && (transferring || state.phase in setOf(UpdatePhase.AVAILABLE, UpdatePhase.FAILED))) AlertDialog(onDismissRequest = dismiss,
        title = { Text(stringResource(R.string.mobile_update_title)) },
        text = { AppUpdateStatus(state) },
        confirmButton = { if (!transferring) TextButton(onClick = download) {
            Text(stringResource(if (state.phase == UpdatePhase.FAILED) R.string.retry else R.string.mobile_update_download))
        } },
        dismissButton = { TextButton(onClick = dismiss) {
            Text(stringResource(if (transferring) R.string.mobile_update_hide else R.string.later))
        } })
}

@Composable internal fun AppUpdateStatus(state: AppUpdateState) {
    Column(Modifier.fillMaxWidth().heightIn(min = 92.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(stringResource(updateStatus(state.phase), state.release?.version.orEmpty()),
            style = MaterialTheme.typography.bodySmall, modifier = Modifier.testTag("apk-update-phase"),
            color = if (state.phase == UpdatePhase.FAILED) MaterialTheme.colorScheme.error else Muted)
        val release = state.release
        if (release != null && state.phase in setOf(UpdatePhase.DOWNLOADING, UpdatePhase.VERIFYING))
            MeasuredTransfer(state.bytes, release.bytes, "apk-transfer")
        else if (state.phase == UpdatePhase.CHECKING) LinearProgressIndicator(Modifier.fillMaxWidth())
    }
}

private fun updateStatus(phase: UpdatePhase) = when (phase) {
    UpdatePhase.IDLE -> R.string.mobile_update_idle
    UpdatePhase.CHECKING -> R.string.mobile_update_checking
    UpdatePhase.CURRENT -> R.string.mobile_update_current
    UpdatePhase.UNPUBLISHED -> R.string.mobile_update_unpublished
    UpdatePhase.AVAILABLE -> R.string.mobile_update_available
    UpdatePhase.DOWNLOADING -> R.string.mobile_update_downloading
    UpdatePhase.VERIFYING -> R.string.mobile_update_verifying
    UpdatePhase.READY -> R.string.mobile_update_ready
    UpdatePhase.PERMISSION -> R.string.mobile_update_permission
    UpdatePhase.FAILED -> R.string.mobile_update_failed
    UpdatePhase.INCOMPATIBLE -> R.string.mobile_update_incompatible
    UpdatePhase.DEVELOPMENT -> R.string.mobile_update_development
}
