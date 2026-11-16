package ru.kmvms.mobile.ui.events

import android.app.TimePickerDialog
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import ru.kmvms.mobile.R
import ru.kmvms.mobile.events.*
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.server.Panel
import ru.kmvms.mobile.data.NasApi

@Composable internal fun EventPreferencesScreen(state: EventsState, save: (EventPreferences) -> Unit, api: NasApi,
    profileId: String?, onMessage: (String) -> Unit) {
    var baseline by remember { mutableStateOf(state.preferences) }
    var value by remember { mutableStateOf(state.preferences) }
    LaunchedEffect(state.preferences) {
        val fresh = state.preferences
        // Other clients can save settings, but must not erase a local draft.
        if (value == baseline || value == fresh || value == null) value = fresh
        baseline = fresh
    }
    val edited = value
    if (edited == null) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
        return
    }
    Column(Modifier.fillMaxSize()) {
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
            if (profileId != null) PushSettings(api, profileId, onMessage)
            EventPersonalSettings(edited, state.cameras, state.busy || state.settingsLoading) { value = it }
        }
        Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 10.dp), horizontalArrangement = Arrangement.End) {
            Button(onClick = { save(edited) }, enabled = !state.busy && !state.settingsLoading && baseline != edited,
                modifier = Modifier.widthIn(min = 150.dp).testTag("events-preferences-save")) { Text(stringResource(R.string.save)) }
        }
    }
}

@Composable internal fun EventPersonalSettings(value: EventPreferences, cameras: List<EventCamera>, busy: Boolean, change: (EventPreferences) -> Unit) {
    Panel {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(stringResource(R.string.events_allow_notifications), modifier = Modifier.weight(1f))
            Switch(value.notifications, { change(value.copy(notifications = it)) }, enabled = !busy, modifier = Modifier.testTag("events-notifications"))
        }
        Text(stringResource(R.string.events_preferences_scope), color = Muted, style = MaterialTheme.typography.bodySmall)
    }
    Panel {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Checkbox(value.hoursEnabled, { change(value.copy(hoursEnabled = it)) }, enabled = !busy, modifier = Modifier.testTag("events-hours-enabled"))
            Text(stringResource(R.string.events_hours), style = MaterialTheme.typography.bodyMedium)
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            ScheduleMode(!value.perCameraHours, R.string.events_schedule_all, !busy, Modifier.weight(1f), "events-schedule-all") { change(value.copy(perCameraHours = false)) }
            ScheduleMode(value.perCameraHours, R.string.events_schedule_per_camera, !busy, Modifier.weight(1f), "events-schedule-per-camera") { change(value.copy(perCameraHours = true)) }
        }
        if (!value.perCameraHours) Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(stringResource(R.string.events_hours_from), color = Muted, style = MaterialTheme.typography.labelSmall)
                EventTime(value.startMinute, stringResource(R.string.events_hours_from), !busy, Modifier.width(64.dp), "events-hours-from") {
                    change(value.copy(hoursEnabled = true, startMinute = it))
                }
            }
            EventTimeSeparator(Modifier.padding(top = 16.dp), "events-hours-separator")
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(stringResource(R.string.events_hours_to), color = Muted, style = MaterialTheme.typography.labelSmall)
                EventTime(value.endMinute, stringResource(R.string.events_hours_to), !busy, Modifier.width(64.dp), "events-hours-to") {
                    change(value.copy(hoursEnabled = true, endMinute = it))
                }
            }
        }
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(stringResource(R.string.cameras), style = MaterialTheme.typography.titleSmall, modifier = Modifier.weight(1f))
            if (value.perCameraHours) {
                Box(Modifier.width(64.dp), contentAlignment = Alignment.Center) { Text(stringResource(R.string.events_hours_from), color = Muted, style = MaterialTheme.typography.labelSmall) }
                Spacer(Modifier.width(14.dp))
                Box(Modifier.width(64.dp), contentAlignment = Alignment.Center) { Text(stringResource(R.string.events_hours_to), color = Muted, style = MaterialTheme.typography.labelSmall) }
            }
        }
        Column {
          cameras.forEach { camera ->
            val selected = value.cameraIds?.contains(camera.id) ?: true
            val hours = value.hoursFor(camera.id)
            Row(Modifier.fillMaxWidth().heightIn(min = 48.dp).testTag("events-pref-row-${camera.id}"), verticalAlignment = Alignment.CenterVertically) {
                Checkbox(selected, { checked ->
                    val prior = value.cameraIds ?: cameras.map { it.id }.toSet()
                    change(value.copy(cameraIds = if (checked) prior + camera.id else prior - camera.id))
                }, enabled = !busy, modifier = Modifier.size(40.dp).testTag("events-pref-camera-${camera.id}"))
                Text(camera.name, modifier = Modifier.weight(1f).padding(end = 6.dp), maxLines = 1, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodySmall)
                if (value.perCameraHours) {
                    EventTime(hours.startMinute, camera.name + ": " + stringResource(R.string.events_hours_from), !busy && selected,
                        Modifier.width(64.dp), "events-camera-from-${camera.id}") {
                        change(value.copy(hoursEnabled = true, cameraSchedules = value.cameraSchedules + (camera.id to hours.copy(startMinute = it))))
                    }
                    EventTimeSeparator(Modifier, "events-camera-separator-${camera.id}")
                    EventTime(hours.endMinute, camera.name + ": " + stringResource(R.string.events_hours_to), !busy && selected,
                        Modifier.width(64.dp), "events-camera-to-${camera.id}") {
                        change(value.copy(hoursEnabled = true, cameraSchedules = value.cameraSchedules + (camera.id to hours.copy(endMinute = it))))
                    }
                }
            }
          }
        }
        Text(stringResource(R.string.events_schedule_help), color = Muted, style = MaterialTheme.typography.bodySmall)
    }
    Panel {
        Text(stringResource(R.string.events_types), style = MaterialTheme.typography.titleMedium)
        CameraEventKind.useful.toList().chunked(2).forEach { row ->
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                row.forEach { kind -> Row(Modifier.weight(1f).heightIn(min = 48.dp), verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(value.kinds?.contains(kind) ?: true, { selected ->
                        val prior = value.kinds ?: CameraEventKind.useful
                        change(value.copy(kinds = if (selected) prior + kind else prior - kind))
                    }, enabled = !busy, modifier = Modifier.testTag("events-pref-kind-${kind.code}"))
                    Text(stringResource(kind.label()), style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f))
                } }
                if (row.size == 1) Spacer(Modifier.weight(1f))
            }
        }
    }
}

@Composable private fun ScheduleMode(selected: Boolean, label: Int, enabled: Boolean, modifier: Modifier, tag: String, choose: () -> Unit) {
    OutlinedButton(onClick = choose, enabled = enabled, modifier = modifier.heightIn(min = 40.dp).testTag(tag),
        contentPadding = PaddingValues(horizontal = 6.dp, vertical = 6.dp),
        colors = ButtonDefaults.outlinedButtonColors(containerColor = if (selected) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surface)) {
        Text(stringResource(label), style = MaterialTheme.typography.labelMedium, maxLines = 2)
    }
}

@Composable private fun EventTimeSeparator(modifier: Modifier, tag: String) {
    val color = Muted.copy(alpha = .55f)
    Canvas(modifier.width(14.dp).height(10.dp).testTag(tag)) {
        val y = size.height / 2
        val end = size.width - 3.dp.toPx()
        val start = 3.dp.toPx()
        val head = 3.dp.toPx()
        val stroke = 1.dp.toPx()
        drawLine(color, Offset(start, y), Offset(end, y), strokeWidth = stroke, cap = StrokeCap.Round)
        drawLine(color, Offset(end - head, y - head), Offset(end, y), strokeWidth = stroke, cap = StrokeCap.Round)
        drawLine(color, Offset(end - head, y + head), Offset(end, y), strokeWidth = stroke, cap = StrokeCap.Round)
    }
}

@Composable private fun EventTime(minutes: Int, label: String, enabled: Boolean, modifier: Modifier, tag: String, change: (Int) -> Unit) {
    val context = LocalContext.current
    // Compact visible bar; its row retains a 48 dp touch area for the native picker.
    Box(modifier.height(48.dp), contentAlignment = Alignment.Center) {
        OutlinedButton(onClick = { TimePickerDialog(context, { _, hour, minute -> change(hour * 60 + minute) }, minutes / 60, minutes % 60, true).show() },
            enabled = enabled, modifier = Modifier.fillMaxWidth().height(32.dp).testTag(tag).semantics { contentDescription = label },
            contentPadding = PaddingValues(horizontal = 4.dp, vertical = 0.dp)) {
            Text("%02d:%02d".format(minutes / 60, minutes % 60), style = MaterialTheme.typography.labelMedium, maxLines = 1)
        }
    }
}
