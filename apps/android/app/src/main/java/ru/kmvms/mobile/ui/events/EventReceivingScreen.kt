package ru.kmvms.mobile.ui.events

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import ru.kmvms.mobile.R
import ru.kmvms.mobile.events.EventCamera
import ru.kmvms.mobile.events.EventsState
import ru.kmvms.mobile.ui.Muted

@Composable internal fun EventReceivingScreen(state: EventsState, change: (Long, Boolean) -> Unit) {
    Column(Modifier.fillMaxSize()) {
        Text(stringResource(R.string.events_receiving_help), modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp),
            color = Muted, style = MaterialTheme.typography.bodySmall)
        LazyColumn(Modifier.fillMaxSize().testTag("events-receiving-list")) {
            items(state.cameras, key = EventCamera::id) { camera ->
                Surface(color = Color.White) {
                    Row(Modifier.fillMaxWidth().heightIn(min = 52.dp).padding(end = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(camera.receiving, { change(camera.id, it) }, enabled = !state.busy && !state.settingsLoading && camera.protocol == "onvif",
                            modifier = Modifier.testTag("events-receiving-camera-${camera.id}"))
                        Text(camera.name, modifier = Modifier.weight(1f).padding(end = 10.dp), style = MaterialTheme.typography.bodyMedium,
                            maxLines = 1, overflow = TextOverflow.Ellipsis)
                        Text(stringResource(if (camera.protocol != "onvif") R.string.events_receiving_unsupported
                            else if (camera.receiving) R.string.events_receiving_on else R.string.events_receiving_off), color = Muted, style = MaterialTheme.typography.labelSmall)
                    }
                }
                HorizontalDivider(color = MaterialTheme.colorScheme.outline)
            }
        }
    }
}
