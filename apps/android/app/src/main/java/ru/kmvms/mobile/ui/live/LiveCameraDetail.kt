package ru.kmvms.mobile.ui.live

import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.media3.common.util.UnstableApi
import ru.kmvms.mobile.R
import ru.kmvms.mobile.core.Stream
import ru.kmvms.mobile.core.CameraStep
import ru.kmvms.mobile.data.Camera
import ru.kmvms.mobile.media.LiveController
import ru.kmvms.mobile.media.VideoStatus
import ru.kmvms.mobile.ptz.PtzCapabilities
import ru.kmvms.mobile.ptz.PtzHandle
import ru.kmvms.mobile.talk.TalkController
import kotlin.math.roundToInt

@UnstableApi
@Composable fun LiveCameraDetail(camera: Camera, capability: PtzCapabilities?, controller: LiveController,
                                 immersive: Boolean, fullscreen: () -> Unit, close: () -> Unit,
                                 message: (String) -> Unit, mayTalk: Boolean = false,
                                 onCameraSwipe: ((CameraStep) -> Unit)? = null) {
    val context = LocalContext.current
    val talk = remember(controller, camera.id, mayTalk) {
        if (mayTalk) TalkController(context.applicationContext, controller.api, camera.id) else null
    }
    if (talk != null) CameraCallLifecycle(talk) { enabled, duck -> controller.talk(camera.id, enabled, duck) }
    val sessions by controller.sessions.collectAsStateWithLifecycle()
    val selections by controller.playback.collectAsStateWithLifecycle()
    val session = sessions[camera.id]
    val playback = selections[camera.id]
    val video = session?.state?.collectAsStateWithLifecycle()?.value
    val handle = remember(controller, camera.id, capability, playback?.ptz) {
        if (playback?.ptz == true && capability?.supported == true) PtzHandle(controller.api, camera.id, capability) else null
    }
    var mode by remember(camera.id, handle) { mutableStateOf(VideoGestureMode.DIGITAL) }
    var collapsed by remember(camera.id) { mutableStateOf(false) }
    var panelAnchor by remember(camera.id) { mutableStateOf<Offset?>(null) }
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    val error = handle?.errors?.collectAsStateWithLifecycle()?.value ?: 0
    val failureText = stringResource(R.string.ptz_failed)
    LaunchedEffect(handle, error) { if (error > 0) message(failureText) }
    DisposableEffect(handle, lifecycle) {
        val observer = LifecycleEventObserver { _, event -> if (event in setOf(Lifecycle.Event.ON_PAUSE, Lifecycle.Event.ON_STOP)) handle?.stop() }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer); handle?.close() }
    }
    val ready = video?.status == VideoStatus.PLAYING
    LaunchedEffect(handle, ready) { if (!ready) handle?.stop() }
    Column(Modifier.fillMaxSize().testTag("live-detail-${camera.id}")
        .background(if (immersive) Color.Black else MaterialTheme.colorScheme.background)) {
        if (!immersive) Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = close) { Icon(Icons.AutoMirrored.Filled.ArrowBack, stringResource(R.string.back)) }
            Text(camera.name, style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f), maxLines = 1)
            TextButton(onClick = fullscreen) { Text(stringResource(R.string.fullscreen)) }
        }
        Box(if (immersive) Modifier.fillMaxSize() else Modifier.fillMaxWidth().aspectRatio(16f / 9f)) {
            CameraVideo(session, Modifier.fillMaxSize(), zoomable = true, mode = mode,
                onTap = { if (!immersive) fullscreen() },
                aspect = if (playback?.stream == Stream.MAIN) camera.mainAspect else camera.subAspect,
                onPoint = { x, y -> if (ready) handle?.point(x, y) },
                onArea = { left, top, right, bottom -> if (ready) handle?.area(left, top, right, bottom) },
                onCameraSwipe = onCameraSwipe)
            LiveCameraCaption(camera.name, video?.hlsFallback == true, Modifier.align(Alignment.CenterStart))
            Row(Modifier.align(Alignment.TopEnd).padding(10.dp).background(Color(0xB3233044), RoundedCornerShape(12.dp)).padding(horizontal = 4.dp),
                verticalAlignment = Alignment.CenterVertically) {
                if (capability?.supported == true) IconButton(onClick = {
                    handle?.stop(); controller.ptz(camera.id, playback?.ptz != true); collapsed = false
                }, modifier = Modifier.testTag("live-ptz")) {
                    PlayerGlyph(PlayerGlyph.PTZ, if (playback?.ptz == true) Color(0xFF60A5FA) else Color.White, stringResource(R.string.ptz_controls))
                }
                if (talk != null) CallButton(talk)
                IconButton(onClick = { controller.sound(camera.id) }, enabled = playback?.talkDuck != true,
                    modifier = Modifier.testTag("live-sound")) {
                    SpeakerIcon(playback?.audible == true,
                        tint = if (playback?.talkDuck == true) Color(0xFF94A3B8) else Color.White)
                }
                TextButton(onClick = { handle?.stop(); controller.selectStream(camera.id,
                    if (playback?.stream == Stream.MAIN && camera.hasSub) Stream.SUB else Stream.MAIN) },
                    modifier = Modifier.testTag("live-stream"), enabled = camera.hasSub) {
                    Text(if (playback?.stream == Stream.MAIN) "Main" else "Sub", color = Color.White)
                }
                if (immersive) IconButton(onClick = close) { Icon(Icons.Default.Close, stringResource(R.string.close), tint = Color.White) }
            }
            if (immersive && handle != null) FloatingPtz(handle, collapsed, { collapsed = it }, mode,
                { mode = it }, ready, panelAnchor, { panelAnchor = it })
            if (talk != null) CallStatus(talk, Modifier.align(Alignment.BottomStart).padding(10.dp))
        }
        if (!immersive && handle != null) Box(Modifier.weight(1f).fillMaxWidth().padding(12.dp), contentAlignment = Alignment.TopCenter) {
            PtzControls(handle, mode, { mode = it }, Modifier.widthIn(max = 420.dp).fillMaxWidth(), ready = ready)
        }
    }
}

@Composable private fun BoxScope.FloatingPtz(handle: PtzHandle, collapsed: Boolean, collapse: (Boolean) -> Unit,
                                            mode: VideoGestureMode, setMode: (VideoGestureMode) -> Unit, ready: Boolean,
                                            position: Offset?, move: (Offset) -> Unit) {
    var viewport by remember { mutableStateOf(IntSize.Zero) }
    var panel by remember { mutableStateOf(IntSize.Zero) }
    val margin = with(LocalDensity.current) { 12.dp.toPx() }
    Box(Modifier.fillMaxSize().onSizeChanged { viewport = it }) {
        val wanted = position ?: Offset(viewport.width-panel.width-margin, viewport.height-panel.height-margin)
        val clamped = Offset(wanted.x.coerceIn(0f, (viewport.width-panel.width).coerceAtLeast(0).toFloat()),
            wanted.y.coerceIn(0f, (viewport.height-panel.height).coerceAtLeast(0).toFloat()))
        val currentAnchor by rememberUpdatedState(position ?: clamped)
        val drag = Modifier.pointerInput(viewport, panel) {
            detectDragGestures(onDragStart = { handle.stop() }) { change, delta ->
                change.consume()
                move(currentAnchor.let { previous -> Offset(
                    (previous.x+delta.x).coerceIn(0f, (viewport.width-panel.width).coerceAtLeast(0).toFloat()),
                    (previous.y+delta.y).coerceIn(0f, (viewport.height-panel.height).coerceAtLeast(0).toFloat())) })
            }
        }
        Box(Modifier.offset { IntOffset(clamped.x.roundToInt(), clamped.y.roundToInt()) }.onSizeChanged { panel = it }) {
            if (collapsed) IconButton(onClick = { collapse(false) }, modifier = Modifier.size(48.dp)
                .background(Color(0xB3233044), RoundedCornerShape(12.dp)).then(drag).testTag("ptz-floating-collapsed")) {
                PlayerGlyph(PlayerGlyph.PTZ, Color.White, stringResource(R.string.ptz_controls))
            } else PtzControls(handle, mode, setMode, Modifier.width(240.dp).heightIn(max = with(LocalDensity.current) { (viewport.height.toDp() - 76.dp).coerceAtLeast(0.dp) }),
                overlay = true, headerDrag = drag, collapse = {
                    move(clamped) // Keep the anchor when the panel changes size.
                    collapse(true)
                }, ready = ready)
        }
    }
}
