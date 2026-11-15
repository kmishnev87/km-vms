package ru.kmvms.mobile.ui.events

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.unit.dp
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.data.ServerProfile
import ru.kmvms.mobile.push.PushStatus
import ru.kmvms.mobile.push.PushStore
import ru.kmvms.mobile.ui.Muted

@Composable internal fun PushSetupOffer(api: NasApi, profile: ServerProfile, onMessage: (String) -> Unit, close: () -> Unit) {
    val context = LocalContext.current
    val store = remember { PushStore(context) }
    val registration = rememberPushRegistration(api, profile.id, onMessage)
    var accepted by rememberSaveable(profile.id) { mutableStateOf(false) }
    PushSetupDialog(profile.name, accepted, registration,
        accept = { store.answerSetup(profile.id); accepted = true; registration.enable() },
        close = { store.answerSetup(profile.id); close() })
}

@Composable internal fun PushSetupDialog(serverName: String, accepted: Boolean, registration: PushRegistrationUi,
    accept: () -> Unit, close: () -> Unit) {
    val pending = registration.busy || registration.status in setOf(PushStatus.REGISTERING, PushStatus.UPLOADING)
    AlertDialog(onDismissRequest = { if (!registration.busy) close() },
        modifier = Modifier.semantics { testTagsAsResourceId = true }.testTag("push-setup-dialog"),
        title = { Text(stringResource(if (accepted) R.string.push_phone else R.string.push_offer_title)) },
        text = {
            if (!accepted) Text(stringResource(R.string.push_offer_message, serverName))
            else Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(stringResource(registration.testFeedback ?: pushStatusText(registration.permission, registration.status)),
                    modifier = Modifier.heightIn(min = 72.dp).testTag("push-setup-status"), color = Muted)
                if (pending) LinearProgressIndicator(Modifier.fillMaxWidth())
                else if (registration.status == PushStatus.READY && registration.permission)
                    OutlinedButton(onClick = registration.test, enabled = !registration.busy,
                        modifier = Modifier.testTag("push-setup-test")) { Text(stringResource(R.string.push_test)) }
                else {
                    OutlinedButton(onClick = registration.enable, modifier = Modifier.testTag("push-setup-retry")) { Text(stringResource(R.string.retry)) }
                    if (registration.permission) TextButton(onClick = registration.chooseService,
                        modifier = Modifier.testTag("push-setup-provider")) { Text(stringResource(R.string.push_choose_service)) }
                }
                Text(stringResource(R.string.push_offer_settings), style = MaterialTheme.typography.bodySmall, color = Muted)
            }
        },
        confirmButton = {
            TextButton(onClick = if (accepted) close else accept, enabled = !registration.busy,
                modifier = Modifier.testTag(if (accepted) "push-setup-close" else "push-setup-accept")) {
                Text(stringResource(if (accepted) R.string.close else R.string.push_offer_enable))
            }
        },
        dismissButton = {
            if (!accepted) TextButton(onClick = close, modifier = Modifier.testTag("push-setup-decline")) {
                Text(stringResource(R.string.push_offer_decline))
            }
        })
}
