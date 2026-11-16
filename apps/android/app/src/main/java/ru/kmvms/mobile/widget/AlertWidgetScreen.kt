package ru.kmvms.mobile.widget

import androidx.compose.foundation.*
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.ApiProblem
import ru.kmvms.mobile.data.ServerProfile
import ru.kmvms.mobile.ui.*

@Composable internal fun AlertWidgetPopup(server: String, state: AlertWidgetState, phoneReady: Boolean,
    retry: () -> Unit, master: (Boolean) -> Unit, camera: (Long, Boolean) -> Unit, close: () -> Unit) {
    // The transparent Activity consumes the first outside tap; no accidental launcher click-through.
    BoxWithConstraints(Modifier.fillMaxSize().background(Color.Black.copy(alpha = .06f))
        .pointerInput(close) { detectTapGestures(onTap = { close() }) }.testTag("widget-outside").systemBarsPadding().padding(16.dp), contentAlignment = Alignment.Center) {
        Surface(modifier = Modifier.widthIn(max = 420.dp).fillMaxWidth().heightIn(max = maxHeight * .88f)
            .pointerInput(Unit) { detectTapGestures(onTap = {}) }.testTag("widget-popup"),
            shape = RoundedCornerShape(18.dp), color = Color.White.copy(alpha = .93f),
            border = BorderStroke(1.dp, Outline), shadowElevation = 6.dp) {
            Column {
                Row(Modifier.fillMaxWidth().padding(start = 14.dp, end = 6.dp, top = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                    Image(painterResource(R.drawable.kmvms_menu_events), null, Modifier.size(30.dp))
                    Column(Modifier.weight(1f).padding(start = 8.dp)) {
                        Text(stringResource(R.string.widget_alerts), style = MaterialTheme.typography.titleSmall)
                        if (server.isNotBlank()) Text(server, maxLines = 1, overflow = TextOverflow.Ellipsis,
                            style = MaterialTheme.typography.bodySmall, color = Muted)
                    }
                    IconButton(onClick = close, modifier = Modifier.testTag("widget-close")) { Icon(Icons.Default.Close, stringResource(R.string.close), tint = Muted) }
                }
                Box(Modifier.fillMaxWidth().height(3.dp)) {
                    if (state.loading || state.busy) LinearProgressIndicator(Modifier.fillMaxSize().testTag("widget-progress"))
                }
                val snapshot = state.snapshot
                val enabled = !state.loading && !state.busy && !state.updateRequired && state.problem !in setOf(ApiProblem.AUTHENTICATION, ApiProblem.FORBIDDEN, ApiProblem.SAVED_SIGN_IN)
                if (snapshot != null) {
                    Row(Modifier.fillMaxWidth().padding(horizontal = 14.dp), verticalAlignment = Alignment.CenterVertically) {
                        Text(stringResource(R.string.events_allow_notifications), style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
                        Switch(snapshot.preferences.notifications, master, enabled = enabled, modifier = Modifier.testTag("widget-master"))
                    }
                    HorizontalDivider(color = Outline, modifier = Modifier.padding(horizontal = 14.dp))
                    LazyColumn(Modifier.weight(1f, fill = false).fillMaxWidth().testTag("widget-cameras"), contentPadding = PaddingValues(horizontal = 10.dp)) {
                        items(snapshot.cameras, key = { it.id }) { item ->
                            val preference = snapshot.preferences
                            val checked = preference.cameraIds?.contains(item.id) ?: true
                            Row(Modifier.fillMaxWidth().height(56.dp)
                                .toggleable(checked, enabled = enabled && preference.notifications, role = Role.Checkbox, onValueChange = { camera(item.id, it) })
                                .testTag("widget-camera-${item.id}"), verticalAlignment = Alignment.CenterVertically) {
                                Checkbox(checked, onCheckedChange = null, enabled = enabled && preference.notifications, modifier = Modifier.size(40.dp))
                                Column(Modifier.weight(1f).padding(end = 4.dp)) {
                                    Text(item.name, maxLines = 1, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodyMedium)
                                    val detail = if (!item.receiving) stringResource(R.string.widget_receiving_off) else if (preference.hoursEnabled) {
                                        val hours = if (preference.perCameraHours) preference.hoursFor(item.id)
                                            else ru.kmvms.mobile.events.EventHours(preference.startMinute, preference.endMinute)
                                        if (hours.startMinute == hours.endMinute) stringResource(R.string.widget_all_day)
                                        else stringResource(R.string.widget_schedule, minuteText(hours.startMinute), minuteText(hours.endMinute))
                                    } else ""
                                    if (detail.isNotEmpty()) Text(detail, maxLines = 1, overflow = TextOverflow.Ellipsis,
                                        style = MaterialTheme.typography.labelSmall, color = Muted)
                                }
                            }
                        }
                        if (snapshot.cameras.isEmpty()) item { Text(stringResource(R.string.cameras), color = Muted, modifier = Modifier.padding(14.dp)) }
                    }
                }
                if (state.problem != null || state.updateRequired) Row(Modifier.fillMaxWidth().padding(horizontal = 14.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text(stringResource(if (state.updateRequired) R.string.widget_update_nas else when (state.problem) {
                        ApiProblem.AUTHENTICATION, ApiProblem.SAVED_SIGN_IN -> R.string.widget_sign_in
                        ApiProblem.FORBIDDEN -> R.string.access_denied
                        ApiProblem.CONNECTION -> R.string.server_unavailable
                        ApiProblem.RATE_LIMIT -> R.string.too_many_attempts
                        else -> R.string.server_error
                    }), color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f).testTag("widget-error"))
                    TextButton(onClick = retry, enabled = !state.loading && !state.busy, modifier = Modifier.testTag("widget-retry")) { Text(stringResource(R.string.retry)) }
                }
                Column(Modifier.padding(horizontal = 14.dp, vertical = 10.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    if (!phoneReady) Text(stringResource(R.string.widget_phone_off), style = MaterialTheme.typography.labelSmall, color = Muted, modifier = Modifier.testTag("widget-phone-warning"))
                    if (snapshot?.preferences?.hoursEnabled == true) Text(stringResource(R.string.widget_schedule_help, snapshot.timezone), style = MaterialTheme.typography.labelSmall, color = Muted)
                    Text(stringResource(R.string.widget_scope), style = MaterialTheme.typography.labelSmall, color = Muted)
                }
            }
        }
    }
}

private fun minuteText(value: Int) = "%02d:%02d".format(java.util.Locale.ROOT, value / 60, value % 60)

@Composable internal fun AlertWidgetSetupButton(profiles: List<ServerProfile>, activeId: String?) {
    var choose by remember { mutableStateOf(false) }
    var manual by remember { mutableStateOf(false) }
    val context = LocalContext.current
    OutlinedButton(onClick = { if (AlertWidgetPin.supported(context)) choose = true else manual = true },
        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp).testTag("add-alert-widget")) {
        Image(painterResource(R.drawable.kmvms_menu_events), null, Modifier.size(26.dp))
        Spacer(Modifier.width(8.dp)); Text(stringResource(R.string.widget_title))
    }
    if (choose) AlertWidgetProfileDialog(profiles, activeId, { profile ->
        val accepted = runCatching { AlertWidgetPin.request(context, profile.id) }.getOrDefault(false)
        if (accepted) choose = false
        accepted
    }, { choose = false })
    if (manual) AlertDialog(onDismissRequest = { manual = false }, title = { Text(stringResource(R.string.widget_title)) },
        text = { Text(stringResource(R.string.widget_pin_manual)) }, confirmButton = {
            TextButton(onClick = { manual = false }) { Text(stringResource(R.string.close)) }
        })
}

@Composable internal fun AlertWidgetProfileDialog(profiles: List<ServerProfile>, initialId: String?,
    choose: (ServerProfile) -> Boolean, dismiss: () -> Unit) {
    var selected by remember { mutableStateOf(initialId?.takeIf { id -> profiles.any { it.id == id } } ?: profiles.firstOrNull()?.id) }
    var failed by remember { mutableStateOf(false) }
    AlertDialog(onDismissRequest = dismiss, title = { Text(stringResource(R.string.widget_title)) }, text = {
        Column {
            Text(stringResource(if (profiles.isEmpty()) R.string.widget_no_servers else R.string.widget_choose_server), style = MaterialTheme.typography.bodySmall, color = Muted)
            Column(Modifier.heightIn(max = 240.dp).verticalScroll(rememberScrollState())) {
                profiles.forEach { profile -> Row(Modifier.fillMaxWidth().heightIn(min = 48.dp).clickable { selected = profile.id; failed = false }
                    .testTag("widget-profile-${profile.id}"), verticalAlignment = Alignment.CenterVertically) {
                    RadioButton(selected == profile.id, onClick = { selected = profile.id; failed = false })
                    Text(profile.name, maxLines = 1, overflow = TextOverflow.Ellipsis)
                } }
            }
            if (failed) Text(stringResource(R.string.widget_sign_in), color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
        }
    }, confirmButton = {
        TextButton(onClick = { profiles.firstOrNull { it.id == selected }?.let { failed = !choose(it) } }, enabled = profiles.any { it.id == selected },
            modifier = Modifier.testTag("widget-pin-confirm")) { Text(stringResource(R.string.widget_add)) }
    }, dismissButton = { TextButton(onClick = dismiss) { Text(stringResource(R.string.cancel)) } })
}
