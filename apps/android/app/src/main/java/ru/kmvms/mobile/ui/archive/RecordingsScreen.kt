package ru.kmvms.mobile.ui.archive

import android.Manifest
import android.app.DatePickerDialog
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.clickable
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
import androidx.compose.runtime.saveable.listSaver
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.media3.common.util.UnstableApi
import ru.kmvms.mobile.R
import ru.kmvms.mobile.archive.*
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.Outline
import ru.kmvms.mobile.ui.DashboardImage
import java.time.LocalDate
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch

@UnstableApi
@Composable fun RecordingsScreen(api: NasApi, canExport: Boolean, canDelete: Boolean, onImmersive: (Boolean) -> Unit,
                                 message: (String) -> Unit) {
    val archiveApi = remember(api) { ArchiveApi(api) }
    val browser = remember(archiveApi) { ArchiveBrowser(archiveApi) }
    val state by browser.state.collectAsStateWithLifecycle()
    var camera by rememberSaveable { mutableStateOf<String?>(null) }
    var date by rememberSaveable { mutableStateOf<String?>(null) }
    var cameraMenu by remember { mutableStateOf(false) }
    var selected by remember { mutableStateOf<Recording?>(null) }
    var deletion by remember { mutableStateOf<List<RecordingDeleteRequest>?>(null) }
    var revealed by rememberSaveable(camera, date, stateSaver = listSaver<Set<String>, String>(
        save = { it.toList() }, restore = { it.toSet() })) { mutableStateOf(emptySet<String>()) }
    var deleting by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val deleteFailure = stringResource(R.string.recording_delete_failed)
    var clips by rememberSaveable { mutableStateOf(false) }
    val positions = remember { mutableMapOf<String, Long>() }
    val context = LocalContext.current
    var pendingDownload by remember(archiveApi) { mutableStateOf<Recording?>(null) }
    var requestingDownload by remember(archiveApi) { mutableStateOf(false) }
    val downloadStarted = stringResource(R.string.download_started)
    val downloadFailed = stringResource(R.string.server_error)
    fun save(recording: Recording) {
        if (requestingDownload) return
        requestingDownload = true
        scope.launch {
            try { downloadRecording(context, archiveApi, recording); message(downloadStarted) }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { message(downloadFailed) }
            finally { requestingDownload = false }
        }
    }
    val storagePermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        val recording = pendingDownload.also { pendingDownload = null }
        if (recording != null) {
            if (granted) save(recording) else message(downloadFailed)
        }
    }
    val playing = selected
    val deleteSelection = state.items.filter { it.identityKey in revealed }
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
                if (canExport) OutlinedButton(onClick = { clips = true }, modifier = Modifier.height(36.dp).testTag("open-clips"),
                    contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp)) {
                    DashboardImage(R.drawable.kmvms_menu_recordings, Modifier.size(24.dp))
                    Spacer(Modifier.width(6.dp))
                    Text(stringResource(R.string.clips), color = MaterialTheme.colorScheme.onSurface, style = MaterialTheme.typography.labelLarge)
                }
                IconButton(onClick = browser::refresh, enabled = !state.loading) { Icon(Icons.Default.Refresh, stringResource(R.string.refresh)) }
            }
            Row(Modifier.fillMaxWidth().padding(horizontal=16.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Box(Modifier.weight(1f)) {
                    OutlinedButton(onClick = { cameraMenu = true }, Modifier.fillMaxWidth().height(48.dp).testTag("recording-camera-picker")) {
                        val chosen = state.cameras.find { it.value == camera }
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
                            Text(chosen?.name ?: stringResource(R.string.all_cameras), maxLines=1, overflow=TextOverflow.Ellipsis,
                                modifier=Modifier.weight(1f, fill=false), textAlign=TextAlign.Center)
                            if(chosen?.deleted == true) Text(" (${stringResource(R.string.deleted_camera)})", color=Muted, style=MaterialTheme.typography.labelSmall)
                        }
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
                }, Modifier.weight(1f).height(48.dp).testTag("recording-date-picker")) {
                    Text(date ?: stringResource(R.string.all_dates), maxLines=1, modifier=Modifier.fillMaxWidth(), textAlign=TextAlign.Center)
                }
            }
            RecordingsSelectionBar(state.total, zoneLabel(state.zone), deleteSelection.size, state.loading, canDelete, deleting,
                deleteSelected = { deletion = deleteSelection.map(::RecordingDeleteRequest) })
            if (state.failed) TextButton(onClick = browser::refresh, Modifier.padding(horizontal=16.dp)) { Text(stringResource(R.string.retry)) }
            LazyColumn(Modifier.fillMaxSize(), contentPadding=PaddingValues(start=16.dp,end=16.dp,top=8.dp,bottom=16.dp), verticalArrangement=Arrangement.spacedBy(10.dp)) {
                if (state.items.isEmpty() && !state.loading) item { Text(stringResource(R.string.no_recordings), color=Muted) }
                items(state.items, key={it.identityKey}) { recording ->
                    RecordingSwipeCard(recording.id.toString(), canDelete, deleting, recording.identityKey in revealed,
                        onRevealedChange = { open -> revealed = if (open) revealed + recording.identityKey else revealed - recording.identityKey },
                        requestDelete = { deletion = listOf(RecordingDeleteRequest(recording)) }) {
                    Surface(modifier=Modifier.fillMaxWidth().testTag("recording-${recording.id}"), shape=RoundedCornerShape(14.dp),
                        color=Color.White, border=androidx.compose.foundation.BorderStroke(1.dp, Outline)) {
                        Row(Modifier.padding(14.dp), verticalAlignment=Alignment.CenterVertically) {
                            Column(Modifier.weight(1f).clickable(enabled=recording.canPlay && !state.loading) { selected=recording }, verticalArrangement=Arrangement.spacedBy(5.dp)) {
                                Text(recording.camera, fontWeight=FontWeight.SemiBold, maxLines=1, overflow=TextOverflow.Ellipsis)
                                Text(recording.displayTime, style=MaterialTheme.typography.bodyMedium)
                                Text(recording.size, color=Muted, style=MaterialTheme.typography.labelMedium)
                                if (!recording.canPlay) Text(stringResource(R.string.video_unavailable), color=MaterialTheme.colorScheme.error, style=MaterialTheme.typography.labelMedium)
                            }
                            if (recording.canDownload) IconButton(
                                onClick = {
                                    if (Build.VERSION.SDK_INT <= 28 && ContextCompat.checkSelfPermission(
                                            context, Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
                                        pendingDownload = recording
                                        storagePermission.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                                    } else save(recording)
                                },
                                enabled = !requestingDownload && pendingDownload == null && !deleting,
                                modifier = Modifier.testTag("download-recording-${recording.id}")
                            ) { Icon(painterResource(R.drawable.ic_recording_download), stringResource(R.string.download),
                                tint = Muted, modifier = Modifier.size(22.dp)) }
                            IconButton(onClick={selected=recording},enabled=recording.canPlay && !state.loading) {
                                Icon(Icons.Default.PlayArrow, stringResource(R.string.play))
                            }
                        }
                    }
                    }
                }
                if (state.more) item { OutlinedButton(onClick=browser::more, enabled=!state.loading, modifier=Modifier.fillMaxWidth()) { Text(stringResource(R.string.load_more)) } }
            }
        }
    }
    deletion?.let { pending ->
        AlertDialog(onDismissRequest={if(!deleting)deletion=null},
            title={Text(stringResource(if(pending.size==1) R.string.recording_delete_title else R.string.recordings_delete_title))},
            text={Text(if(pending.size==1) stringResource(R.string.recording_delete_confirmation,pending.single().recording.camera,pending.single().recording.displayTime)
                else stringResource(R.string.recordings_delete_confirmation,pending.size))},
            confirmButton={TextButton(enabled=!deleting && canDelete,onClick={
                if(!canDelete || deleting)return@TextButton
                deleting=true
                scope.launch {
                    try {
                        val result = deleteSelectedRecordings(pending, archiveApi::deleteRecording)
                        revealed = revealed - result.removed.map { it.recording.identityKey }.toSet()
                        deletion = result.failed.takeIf { it.isNotEmpty() }
                        browser.refresh()
                        if(result.failed.isNotEmpty()) message(deleteFailure)
                    }
                    catch(cancelled:CancellationException){throw cancelled}
                    catch(_:Exception){message(deleteFailure)}
                    finally{deleting=false}
                }
            },modifier=Modifier.testTag("recording-delete-confirm")){Text(stringResource(R.string.remove))}},
            dismissButton={TextButton(enabled=!deleting,onClick={deletion=null}){Text(stringResource(R.string.cancel))}})
    }
}
