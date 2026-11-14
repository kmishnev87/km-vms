package ru.kmvms.mobile.ui.archive

import android.app.DatePickerDialog
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.media3.common.util.UnstableApi
import ru.kmvms.mobile.R
import ru.kmvms.mobile.archive.*
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.Outline
import java.time.LocalDate

@UnstableApi
@Composable fun RecordingsScreen(api: NasApi, canExport: Boolean, onImmersive: (Boolean) -> Unit,
                                 message: (String) -> Unit) {
    val archiveApi = remember(api) { ArchiveApi(api) }
    val browser = remember(archiveApi) { ArchiveBrowser(archiveApi) }
    val state by browser.state.collectAsStateWithLifecycle()
    var camera by rememberSaveable { mutableStateOf<String?>(null) }
    var date by rememberSaveable { mutableStateOf<String?>(null) }
    var cameraMenu by remember { mutableStateOf(false) }
    var selected by remember { mutableStateOf<Recording?>(null) }
    var clips by rememberSaveable { mutableStateOf(false) }
    val positions = remember { mutableMapOf<String, Long>() }
    val context = LocalContext.current
    val playing = selected
    LaunchedEffect(browser, camera, date) { browser.search(camera, date?.let(LocalDate::parse)) }
    DisposableEffect(browser) { onDispose { browser.close() } }
    BackHandler(selected != null || clips) { if (selected != null) selected = null else clips = false }
    when {
        playing != null -> RecordingPlayer(api, playing, positions[playing.identityKey] ?: 0,
            { position -> positions[playing.identityKey] = position }, { selected = null }, onImmersive)
        clips -> ClipsScreen(archiveApi, state.exportable, state.zone,
            { clips = false }, message)
        else -> Column(Modifier.fillMaxSize()) {
            Row(Modifier.fillMaxWidth().padding(start=16.dp, end=8.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(stringResource(R.string.recordings), style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
                if (canExport) TextButton(onClick = { clips = true }, modifier = Modifier.testTag("open-clips")) { Text(stringResource(R.string.clips)) }
                IconButton(onClick = browser::refresh, enabled = !state.loading) { Icon(Icons.Default.Refresh, stringResource(R.string.refresh)) }
            }
            Row(Modifier.fillMaxWidth().padding(horizontal=16.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Box(Modifier.weight(1f)) {
                    OutlinedButton(onClick = { cameraMenu = true }, Modifier.fillMaxWidth().height(48.dp).testTag("recording-camera-picker")) {
                        val chosen = state.cameras.find { it.value == camera }
                        Text(chosen?.name ?: stringResource(R.string.all_cameras), maxLines=1, overflow=TextOverflow.Ellipsis, modifier=Modifier.weight(1f))
                        if(chosen?.deleted == true) Text(" (${stringResource(R.string.deleted_camera)})", color=Muted, style=MaterialTheme.typography.labelSmall)
                    }
                    DropdownMenu(cameraMenu, { cameraMenu = false }, modifier=Modifier.heightIn(max=360.dp).widthIn(min=224.dp,max=360.dp).testTag("recording-camera-menu")) {
                        DropdownMenuItem(text = { Text(stringResource(R.string.all_cameras),style=MaterialTheme.typography.bodySmall) }, modifier=Modifier.height(44.dp), onClick = { camera=null; cameraMenu=false })
                        state.cameras.forEachIndexed { index, item ->
                            if(item.deleted && state.cameras.getOrNull(index-1)?.deleted != true) {
                                HorizontalDivider(Modifier.padding(horizontal=12.dp,vertical=4.dp))
                                Text(stringResource(R.string.retained_archive_cameras),color=Muted,style=MaterialTheme.typography.labelSmall,modifier=Modifier.padding(horizontal=12.dp,vertical=4.dp).testTag("recording-retained-divider"))
                            }
                            DropdownMenuItem(modifier=Modifier.height(44.dp).testTag("recording-camera-${item.id}"), text = {
                                Row(verticalAlignment=Alignment.CenterVertically) {
                                    Text(item.name,modifier=Modifier.weight(1f),maxLines=1,overflow=TextOverflow.Ellipsis,style=MaterialTheme.typography.bodySmall)
                                    if(item.deleted) Text(" (${stringResource(R.string.deleted_camera)})",color=Muted,style=MaterialTheme.typography.labelSmall)
                                }
                            }, onClick = { camera=item.value; cameraMenu=false })
                        }
                    }
                }
                OutlinedButton(onClick = {
                    val day = date?.let(LocalDate::parse) ?: LocalDate.now(state.zone)
                    DatePickerDialog(context, { _, year, month, dayOfMonth -> date=LocalDate.of(year,month+1,dayOfMonth).toString() },
                        day.year, day.monthValue-1, day.dayOfMonth).apply {
                        setButton(android.content.DialogInterface.BUTTON_NEUTRAL, context.getString(R.string.all_dates)) { _, _ -> date=null }
                    }.show()
                }, Modifier.weight(1f).height(48.dp)) { Text(date ?: stringResource(R.string.all_dates), maxLines=1) }
            }
            Box(Modifier.fillMaxWidth().height(28.dp).padding(horizontal=16.dp), contentAlignment = Alignment.CenterStart) {
                if (state.loading) LinearProgressIndicator(Modifier.fillMaxWidth())
                else Text(stringResource(R.string.archive_count, state.total, zoneLabel(state.zone)), color=Muted, style=MaterialTheme.typography.labelSmall)
            }
            if (state.failed) TextButton(onClick = browser::refresh, Modifier.padding(horizontal=16.dp)) { Text(stringResource(R.string.retry)) }
            LazyColumn(Modifier.fillMaxSize(), contentPadding=PaddingValues(16.dp), verticalArrangement=Arrangement.spacedBy(10.dp)) {
                if (state.items.isEmpty() && !state.loading) item { Text(stringResource(R.string.no_recordings), color=Muted) }
                items(state.items, key={it.identityKey}) { recording ->
                    Surface(onClick = { selected=recording }, enabled=recording.canPlay && !state.loading,
                        modifier=Modifier.fillMaxWidth().testTag("recording-${recording.id}"), shape=RoundedCornerShape(14.dp),
                        color=Color.White, border=androidx.compose.foundation.BorderStroke(1.dp, Outline)) {
                        Row(Modifier.padding(14.dp), verticalAlignment=Alignment.CenterVertically) {
                            Column(Modifier.weight(1f), verticalArrangement=Arrangement.spacedBy(5.dp)) {
                                Text(recording.camera, fontWeight=FontWeight.SemiBold, maxLines=1, overflow=TextOverflow.Ellipsis)
                                Text(recording.displayTime, style=MaterialTheme.typography.bodyMedium)
                                Text(recording.size, color=Muted, style=MaterialTheme.typography.labelMedium)
                                if (!recording.canPlay) Text(stringResource(R.string.video_unavailable), color=MaterialTheme.colorScheme.error, style=MaterialTheme.typography.labelMedium)
                            }
                            Icon(Icons.Default.PlayArrow, stringResource(R.string.play))
                        }
                    }
                }
                if (state.more) item { OutlinedButton(onClick=browser::more, enabled=!state.loading, modifier=Modifier.fillMaxWidth()) { Text(stringResource(R.string.load_more)) } }
            }
        }
    }
}
