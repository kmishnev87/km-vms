package ru.kmvms.mobile.ui.events

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.push.PushStatus
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.server.Panel

@Composable fun PushSettings(api: NasApi, profileId: String, onMessage: (String) -> Unit) {
    val registration = rememberPushRegistration(api, profileId, onMessage)
    Panel {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(stringResource(R.string.push_phone), style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
            Switch(registration.binding?.enabled == true, { enabled ->
                if (enabled) registration.enable() else registration.disable()
            }, enabled = !registration.busy, modifier = Modifier.testTag("push-enable"))
        }
        Text(stringResource(pushStatusText(registration.permission, registration.status)), style = MaterialTheme.typography.bodySmall,
            color = Muted, modifier = Modifier.testTag("push-status"))
        Text(stringResource(R.string.push_scope), style = MaterialTheme.typography.bodySmall, color = Muted)
        TextButton(onClick = registration.chooseService, enabled = registration.permission && !registration.busy,
            modifier = Modifier.testTag("push-provider")) { Text(stringResource(R.string.push_choose_service)) }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (registration.binding?.enabled == true) OutlinedButton(onClick = registration.enable, enabled = !registration.busy,
                modifier = Modifier.weight(1f).testTag("push-retry")) { Text(stringResource(R.string.retry)) }
            OutlinedButton(onClick = registration.test,
                enabled = registration.status == PushStatus.READY && registration.permission && !registration.busy,
                modifier = Modifier.weight(1f).testTag("push-test")) { Text(stringResource(R.string.push_test)) }
        }
    }
}
