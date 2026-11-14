package ru.kmvms.mobile.ui.events

import android.app.TimePickerDialog
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import ru.kmvms.mobile.R
import ru.kmvms.mobile.events.*
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.server.Panel
import ru.kmvms.mobile.data.NasApi

@Composable internal fun EventPreferencesScreen(state: EventsState, save: (EventPreferences) -> Unit, api: NasApi,
    profileId: String?, onMessage: (String) -> Unit) {
    val baseline = state.preferences
    var value by remember(baseline) { mutableStateOf(baseline) }
    val edited = value
    if (edited == null) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
        return
    }
    Column(Modifier.fillMaxSize()) {
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
            if (profileId != null) PushSettings(api, profileId, onMessage)
            Panel {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(stringResource(R.string.events_allow_notifications), modifier = Modifier.weight(1f))
                    Switch(edited.notifications, { value = edited.copy(notifications = it) }, enabled = !state.busy, modifier = Modifier.testTag("events-notifications"))
                }
                Text(stringResource(R.string.events_preferences_scope), color = Muted, style = MaterialTheme.typography.bodySmall)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(edited.hoursEnabled, { value = edited.copy(hoursEnabled = it) }, enabled = !state.busy, modifier = Modifier.testTag("events-hours-enabled"))
                    Text(stringResource(R.string.events_hours), style = MaterialTheme.typography.bodyMedium)
                }
                Row(Modifier.widthIn(max = 336.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    EventTime(edited.startMinute, R.string.events_hours_from, edited.hoursEnabled && !state.busy, Modifier.weight(1f), "events-hours-from") { value = edited.copy(startMinute = it) }
                    EventTime(edited.endMinute, R.string.events_hours_to, edited.hoursEnabled && !state.busy, Modifier.weight(1f), "events-hours-to") { value = edited.copy(endMinute = it) }
                }
            }
            Panel {
                Text(stringResource(R.string.cameras), style = MaterialTheme.typography.titleMedium)
                state.cameras.forEach { camera ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(edited.cameraIds?.contains(camera.id) ?: true, { selected ->
                            val prior = edited.cameraIds ?: state.cameras.map { it.id }.toSet()
                            value = edited.copy(cameraIds = if (selected) prior + camera.id else prior - camera.id)
                        }, enabled = !state.busy, modifier = Modifier.testTag("events-pref-camera-${camera.id}"))
                        Text(camera.name, modifier = Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium)
                    }
                }
            }
            Panel {
                Text(stringResource(R.string.events_types), style = MaterialTheme.typography.titleMedium)
                CameraEventKind.useful.toList().chunked(2).forEach { row ->
                    Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                        row.forEach { kind -> Row(Modifier.weight(1f).heightIn(min = 48.dp), verticalAlignment = Alignment.CenterVertically) {
                            Checkbox(edited.kinds?.contains(kind) ?: true, { selected ->
                                val prior = edited.kinds ?: CameraEventKind.useful
                                value = edited.copy(kinds = if (selected) prior + kind else prior - kind)
                            }, enabled = !state.busy, modifier = Modifier.testTag("events-pref-kind-${kind.code}"))
                            Text(stringResource(kind.label()), style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f))
                        } }
                        if (row.size == 1) Spacer(Modifier.weight(1f))
                    }
                }
            }
        }
        Row(Modifier.fillMaxWidth().padding(16.dp), horizontalArrangement = Arrangement.End) {
            Button(onClick = { save(edited) }, enabled = !state.busy && baseline != edited,
                modifier = Modifier.widthIn(min = 150.dp).testTag("events-preferences-save")) { Text(stringResource(R.string.save)) }
        }
    }
}

@Composable private fun EventTime(minutes: Int, label: Int, enabled: Boolean, modifier: Modifier, tag: String, change: (Int) -> Unit) {
    val context = LocalContext.current
    OutlinedButton(onClick = { TimePickerDialog(context, { _, hour, minute -> change(hour * 60 + minute) }, minutes / 60, minutes % 60, true).show() },
        enabled = enabled, modifier = modifier.heightIn(min = 48.dp).testTag(tag), contentPadding = PaddingValues(horizontal = 10.dp)) {
        Text(stringResource(label) + " " + "%02d:%02d".format(minutes / 60, minutes % 60), modifier = Modifier.weight(1f), maxLines = 1)
        EventActionIcon(false, MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
