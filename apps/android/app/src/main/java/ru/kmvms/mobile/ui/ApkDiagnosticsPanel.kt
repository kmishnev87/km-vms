package ru.kmvms.mobile.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import ru.kmvms.mobile.R
import ru.kmvms.mobile.diagnostics.*

@Composable fun ApkDiagnosticsPanel(controller: ApkReportController = viewModel()) {
    var open by rememberSaveable { mutableStateOf(false) }
    var comment by rememberSaveable { mutableStateOf("") }
    val state by controller.state.collectAsStateWithLifecycle()
    Surface(shape = RoundedCornerShape(14.dp), color = MaterialTheme.colorScheme.surface) {
        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(stringResource(R.string.apk_report_title), style = MaterialTheme.typography.titleMedium)
            Text(stringResource(R.string.apk_report_scope), style = MaterialTheme.typography.bodySmall, color = Muted)
            OutlinedButton(onClick = { controller.reset(); open = true }, modifier = Modifier.testTag("apk-report-open")) {
                Icon(Icons.AutoMirrored.Filled.Send, null, Modifier.size(18.dp)); Spacer(Modifier.width(8.dp))
                Text(stringResource(R.string.apk_report_open))
            }
        }
    }
    if (open) {
        val busy = state.phase in setOf(ApkReportPhase.PREPARING, ApkReportPhase.SENDING)
        val accepted = state.phase == ApkReportPhase.ACCEPTED
        AlertDialog(onDismissRequest = { if (!busy) open = false },
            title = { Text(stringResource(R.string.apk_report_title)) },
            text = { Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                if (accepted) {
                    Text(stringResource(R.string.apk_report_accepted))
                    SelectionContainer { Text(state.receipt.orEmpty(), modifier = Modifier.testTag("apk-report-receipt")) }
                } else {
                    Text(stringResource(R.string.apk_report_explanation), style = MaterialTheme.typography.bodySmall, color = Muted)
                    OutlinedTextField(comment, { if (it.length <= 4000) comment = it }, label = { Text(stringResource(R.string.apk_report_comment)) },
                        minLines = 3, maxLines = 5, enabled = !busy, modifier = Modifier.fillMaxWidth().testTag("apk-report-comment"))
                    Text(stringResource(R.string.apk_report_consent), style = MaterialTheme.typography.bodySmall, color = Muted)
                    if (busy) {
                        if (state.phase == ApkReportPhase.SENDING && state.total > 0) LinearProgressIndicator(
                            progress = { state.bytes.toFloat() / state.total }, modifier = Modifier.fillMaxWidth())
                        else LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                        Text(stringResource(if (state.phase == ApkReportPhase.PREPARING) R.string.apk_report_preparing
                            else if (state.bytes >= state.total) R.string.apk_report_waiting else R.string.apk_report_sending), style = MaterialTheme.typography.bodySmall)
                    }
                    if (state.phase == ApkReportPhase.FAILED) Text(stringResource(when (state.errorStatus) {
                        429 -> R.string.apk_report_busy; 507 -> R.string.apk_report_full; else -> R.string.apk_report_failed
                    }), color = MaterialTheme.colorScheme.error, modifier = Modifier.testTag("apk-report-error"))
                }
            } },
            confirmButton = {
                if (accepted) TextButton(onClick = { open = false; comment = ""; controller.reset() }, modifier = Modifier.testTag("apk-report-done")) { Text(stringResource(R.string.close)) }
                else TextButton(onClick = { controller.send(comment) }, enabled = !busy, modifier = Modifier.testTag("apk-report-send")) { Text(stringResource(R.string.apk_report_send)) }
            },
            dismissButton = { if (!accepted) TextButton(onClick = { open = false }, enabled = !busy, modifier = Modifier.testTag("apk-report-cancel")) { Text(stringResource(R.string.cancel)) } })
    }
}
