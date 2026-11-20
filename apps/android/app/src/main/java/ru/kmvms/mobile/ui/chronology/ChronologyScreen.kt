package ru.kmvms.mobile.ui.chronology

import android.app.Activity
import android.app.DatePickerDialog
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.media3.common.util.UnstableApi
import ru.kmvms.mobile.R
import ru.kmvms.mobile.chronology.*
import ru.kmvms.mobile.core.TimelinePhase
import ru.kmvms.mobile.core.CameraStep
import ru.kmvms.mobile.core.adjacentCamera
import ru.kmvms.mobile.data.Camera
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.events.MobileVideoRequest
import ru.kmvms.mobile.media.VideoState
import ru.kmvms.mobile.media.VideoStatus
import ru.kmvms.mobile.ui.Accent
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.Outline
import ru.kmvms.mobile.ui.SoftAccent
import ru.kmvms.mobile.ui.archive.ClipTimeDialog
import ru.kmvms.mobile.archive.zoneLabel
import ru.kmvms.mobile.ui.live.SpeakerIcon
import ru.kmvms.mobile.ui.live.VideoSurface
import ru.kmvms.mobile.ui.live.CameraSelectionButton
import ru.kmvms.mobile.ui.live.OrderedCameraPicker
import ru.kmvms.mobile.ui.live.VideoTransform
import ru.kmvms.mobile.ui.live.VideoGestureMode
import ru.kmvms.mobile.ui.live.videoGestures
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter

@UnstableApi
@Composable fun ChronologyScreen(api: NasApi, cameras: List<Camera>, immersive: (Boolean) -> Unit, message: (String) -> Unit,
                                initial: MobileVideoRequest? = null) {
    val context = LocalContext.current
    var savedPosition by rememberSaveable { mutableLongStateOf(System.currentTimeMillis() - 5 * 60_000) }
    var savedZone by rememberSaveable { mutableStateOf("UTC") }
    var savedDay by rememberSaveable { mutableStateOf(Instant.ofEpochMilli(savedPosition).atZone(ZoneId.of(savedZone)).toLocalDate().toString()) }
    val controller = remember(api) { ChronologyController(context, ChronologyApi(api), savedPosition, ZoneId.of(savedZone), LocalDate.parse(savedDay)) }
    val state by controller.state.collectAsStateWithLifecycle()
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    var chosen by rememberSaveable { mutableStateOf(listOf<Long>()) }
    var chooseCameras by remember { mutableStateOf(false) }
    var chooseTime by remember { mutableStateOf(false) }
    var fullscreen by rememberSaveable { mutableStateOf<Long?>(null) }
    val moment = Instant.ofEpochMilli(state.position).atZone(state.zone)
    val configuration = LocalConfiguration.current
    val compact = configuration.screenHeightDp < 500 && configuration.screenWidthDp > configuration.screenHeightDp
    val failure = stringResource(R.string.server_error)
    var archiveCameras by remember(api) { mutableStateOf(cameras) }
    LaunchedEffect(api, cameras) {
        try { archiveCameras = ChronologyApi(api).cameras() }
        catch (cancelled: kotlinx.coroutines.CancellationException) { throw cancelled }
        catch (_: Exception) { message(failure) }
    }
    SideEffect { savedPosition = state.position; savedZone = state.zone.id; savedDay = state.day.toString() }
    LaunchedEffect(controller, chosen) { controller.select(chosen) }
    LaunchedEffect(initial?.key) {
        initial?.archiveTime?.let { position ->
            chosen = listOf(initial.cameraId)
            controller.seek(position)
            fullscreen = initial.cameraId
            controller.play(true)
        }
    }
    LaunchedEffect(state.failures) { if (state.failures > 0) message(failure) }
    DisposableEffect(controller, lifecycle) {
        controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED))
        val observer = LifecycleEventObserver { _, _ -> controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer); controller.close() }
    }
    LaunchedEffect(fullscreen) { immersive(fullscreen != null) }
    DisposableEffect(Unit) { onDispose { immersive(false) } }
    BackHandler(fullscreen != null) { fullscreen = null }

    val expanded = fullscreen
    if (expanded != null && expanded in state.selected) {
        key(expanded) { TimelineFullscreen(controller, state, expanded,
            onCameraSwipe = if (state.selected.size > 1) { step ->
                adjacentCamera(state.selected, expanded, step)?.let { fullscreen = it }
            } else null) { fullscreen = null } }
    } else Column(Modifier.fillMaxSize()) {
        Surface(color = Color.White) {
            Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                if (!compact) Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text(stringResource(R.string.chronology), style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
                    CameraSelectionButton(chosen.size, archiveCameras.size, { chooseCameras = true }, Modifier.testTag("timeline-cameras"))
                    IconButton(onClick = controller::refreshRanges, enabled = !state.loadingRanges) { Icon(Icons.Default.Refresh, stringResource(R.string.refresh)) }
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                    OutlinedButton(onClick = {
                        val day = state.day
                        DatePickerDialog(context, { _, year, month, date -> controller.day(LocalDate.of(year, month + 1, date)) }, day.year, day.monthValue - 1, day.dayOfMonth).show()
                    }, modifier = (if (compact) Modifier.width(120.dp) else Modifier.weight(1f)).testTag("timeline-date")) { Text(state.day.format(DateTimeFormatter.ofPattern("dd.MM.yyyy"))) }
                    OutlinedButton(onClick = { chooseTime = true }, modifier = (if (compact) Modifier.width(120.dp) else Modifier.weight(1f)).testTag("timeline-time")) {
                        Text(moment.format(DateTimeFormatter.ofPattern("HH:mm:ss")), maxLines = 1)
                    }
                    if (compact) {
                        TimelineControls(controller, state, Modifier.weight(1f))
                        CameraSelectionButton(chosen.size, archiveCameras.size, { chooseCameras = true }, Modifier.testTag("timeline-cameras"))
                        IconButton(onClick = controller::refreshRanges, enabled = !state.loadingRanges) { Icon(Icons.Default.Refresh, stringResource(R.string.refresh)) }
                    }
                }
                if (!compact) TimelineControls(controller, state)
                TimelineRuler(state, archiveCameras, controller::seek,controller::rangeWindow)
            }
        }
        LazyColumn(Modifier.fillMaxSize().testTag("timeline-videos"), contentPadding = PaddingValues(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            if (chosen.isEmpty()) item { TextButton(onClick = { chooseCameras = true }, modifier = Modifier.fillMaxWidth().height(120.dp)) { Text(stringResource(R.string.timeline_add_cameras)) } }
            items(state.selected.chunked(if (compact) 2 else 1), key = { it.joinToString(",") }) { row ->
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                row.forEach { id -> key(id) { Column(Modifier.weight(1f)) {
                    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth().padding(start = 4.dp)) {
                        Text(archiveCameras.find { it.id == id }?.let { it.name + if(it.deleted) " (${stringResource(R.string.deleted_camera)})" else "" } ?: initial?.takeIf { it.cameraId == id }?.cameraName.orEmpty(), maxLines = 1, overflow = TextOverflow.Ellipsis,
                            style = MaterialTheme.typography.labelLarge, modifier = Modifier.weight(1f))
                        IconButton(onClick = { chosen = chosen - id }, modifier = Modifier.size(36.dp).testTag("timeline-close-$id")) { Icon(Icons.Default.Close, stringResource(R.string.close), modifier = Modifier.size(18.dp)) }
                    }
                    TimelineVideo(controller, state, id, Modifier.fillMaxWidth().aspectRatio(16f / 9f).clip(RoundedCornerShape(8.dp))) { fullscreen = id }
                } } }
                if (compact && row.size == 1) Spacer(Modifier.weight(1f))
                }
            }
        }
    }
    if (chooseCameras) OrderedCameraPicker(archiveCameras, chosen, { chooseCameras = false }, includeRetained = true, tagPrefix = "timeline") { selected -> chosen = selected; chooseCameras = false }
    if (chooseTime) ClipTimeDialog("timeline", moment.toLocalTime(), { chooseTime = false }) { time ->
        controller.seek(state.day.atTime(time).atZone(state.zone).toInstant().toEpochMilli()); chooseTime = false
    }
}

@UnstableApi
@Composable private fun TimelineControls(controller: ChronologyController, state: ChronologyState, modifier: Modifier = Modifier) {
    val label = stringResource(if (state.wantsPlay) R.string.pause else R.string.play)
    Row(modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        FilledTonalIconButton(onClick = controller::toggle, enabled = state.selected.isNotEmpty(),
            modifier = Modifier.testTag("timeline-play").semantics { contentDescription = label },
            colors = IconButtonDefaults.filledTonalIconButtonColors(containerColor = if (state.wantsPlay) SoftAccent else Color.Transparent)) {
            if (state.wantsPlay) Text("Ⅱ", style = MaterialTheme.typography.titleLarge, color = Accent)
            else Icon(Icons.Default.PlayArrow, label)
        }
        IconButton(onClick = controller::nextRecording, enabled = state.selected.isNotEmpty() && !state.findingNext,
            modifier = Modifier.size(40.dp).testTag("timeline-next-recording")) {
            Icon(painterResource(R.drawable.ic_timeline_next), stringResource(R.string.timeline_next_recording),
                tint = Accent, modifier = Modifier.size(20.dp))
        }
        var speedMenu by remember { mutableStateOf(false) }
        Box {
            TextButton(onClick = { speedMenu = true }, modifier = Modifier.testTag("timeline-speed")) { Text("${state.speed.toString().removeSuffix(".0")}×") }
            DropdownMenu(speedMenu, { speedMenu = false }) {
                listOf(.25f, .5f, 1f, 2f, 4f).forEach { speed -> DropdownMenuItem(text = { Text("${speed.toString().removeSuffix(".0")}×") },
                    onClick = { controller.speed(speed); speedMenu = false }) }
            }
        }
        Box(Modifier.weight(1f).heightIn(min = 40.dp), contentAlignment = Alignment.CenterStart) {
            if(state.wantsPlay && state.waiting.isNotEmpty())
                Row(verticalAlignment=Alignment.CenterVertically,horizontalArrangement=Arrangement.spacedBy(6.dp)) {
                    CircularProgressIndicator(Modifier.size(14.dp),strokeWidth=2.dp)
                    Text(stringResource(R.string.timeline_buffering),color=Muted,style=MaterialTheme.typography.labelSmall,modifier=Modifier.testTag("timeline-state"))
                }
            else if(state.noNextRecording) Text(stringResource(R.string.timeline_no_next),color=Muted,style=MaterialTheme.typography.labelSmall,modifier=Modifier.testTag("timeline-state"))
            else if(!state.wantsPlay) Text(stringResource(R.string.timeline_paused),color=Muted,style=MaterialTheme.typography.labelSmall,modifier=Modifier.testTag("timeline-state"))
        }
        TextButton(onClick = controller::firstRecording, enabled = state.ranges.values.any { it.isNotEmpty() }, modifier = Modifier.testTag("timeline-first")) { Text(stringResource(R.string.timeline_find)) }
    }
}

@UnstableApi
@Composable internal fun TimelineVideo(controller: ChronologyController, state: ChronologyState, id: Long, modifier: Modifier,
    onCameraSwipe: ((CameraStep) -> Unit)? = null, expand: () -> Unit) {
    val track = controller.track(id)
    val phase = state.phases[id] ?: TimelinePhase.RESOLVING
    val emptyTransform = remember(id) { VideoTransform() }
    var size by remember { mutableStateOf(IntSize.Zero) }
    val swipe by rememberUpdatedState(onCameraSwipe)
    val tap by rememberUpdatedState(expand)
    if (phase == TimelinePhase.EMPTY) Box(modifier.background(Color.Black).testTag("timeline-empty-$id")
        .onSizeChanged { size = it }.videoGestures(emptyTransform, VideoGestureMode.DIGITAL, false, size, 16f/9f,
            { tap() }, { _, _ -> }, { _, _, _, _ -> }, if (onCameraSwipe != null) { step -> swipe?.invoke(step) } else null),
        contentAlignment = Alignment.Center) {
        Text(stringResource(R.string.timeline_no_recording), color = Color.White, style = MaterialTheme.typography.bodySmall)
    } else VideoSurface(track?.player,
        VideoState(when { phase==TimelinePhase.FAILED -> VideoStatus.FAILED
            state.wantsPlay && !state.running && id in state.waiting -> VideoStatus.BUFFERING
            phase==TimelinePhase.READY -> VideoStatus.PLAYING
            else -> VideoStatus.BUFFERING }, unsupportedCodec = track?.unsupportedCodec() == true),
        controller::retry, modifier.testTag("timeline-video-$id"), zoomable = true, onTap = expand, onCameraSwipe = onCameraSwipe)
}
