package ru.kmvms.mobile.ui.live

import android.app.Activity
import android.content.res.Configuration
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shadow
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.sp
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.dp
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.repeatOnLifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.media3.common.util.UnstableApi
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.combine
import ru.kmvms.mobile.push.localLiveAttention
import ru.kmvms.mobile.media.VideoStatus
import ru.kmvms.mobile.R
import ru.kmvms.mobile.core.Stream
import ru.kmvms.mobile.core.VisibleTile
import ru.kmvms.mobile.core.visibleCameraIds
import ru.kmvms.mobile.core.LiveCameraOrder
import ru.kmvms.mobile.data.Camera
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.events.MobileVideoRequest
import ru.kmvms.mobile.media.LiveController
import ru.kmvms.mobile.media.LiveViewPreferences
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.PageBackground
import ru.kmvms.mobile.ptz.PtzCapabilities

@UnstableApi
@Composable fun LiveScreen(api: NasApi, cameras: List<Camera>, capabilities: Map<Long, PtzCapabilities>,
                           onMessage: (String) -> Unit, onImmersive: (Boolean) -> Unit, initial: MobileVideoRequest? = null,
                           profileId: String, onExit: () -> Unit = {}) {
    val context = LocalContext.current
    val controller = remember(api) { LiveController(context.applicationContext, api) }
    val sessions by controller.sessions.collectAsStateWithLifecycle()
    val selections by controller.playback.collectAsStateWithLifecycle()
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    val attentionOwner = remember(controller) { Any() }
    LaunchedEffect(sessions, lifecycle, profileId) {
        localLiveAttention.clear(attentionOwner)
        val entries = sessions.entries.toList()
        if (entries.isEmpty()) return@LaunchedEffect
        try {
            lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
                try {
                    combine(entries.map { it.value.state }) { values ->
                        entries.indices.filter { index -> values[index].status == VideoStatus.PLAYING &&
                            entries[index].value.player.isPlaying }.map { entries[it].key }.toSet()
                    }.distinctUntilChanged().collect { localLiveAttention.update(attentionOwner, profileId, it) }
                } finally { localLiveAttention.clear(attentionOwner) }
            }
        } finally { localLiveAttention.clear(attentionOwner) }
    }
    DisposableEffect(attentionOwner) { onDispose { localLiveAttention.clear(attentionOwner) } }
    val list = rememberLazyListState()
    val preferences = remember(context) { LiveViewPreferences(context.applicationContext) }
    var savedOrder by remember(profileId) { mutableStateOf(preferences.load(profileId)) }
    var choosing by rememberSaveable { mutableStateOf(false) }
    val singleId = initial?.cameraId?.takeIf { initial.singleCamera }
    val viewingIds = LiveCameraOrder.viewing(savedOrder, cameras.map { it.id }, singleId)
    val viewingCameras = viewingIds.mapNotNull { id -> cameras.find { it.id == id } }
    var selectedId by rememberSaveable { mutableStateOf(initial?.cameraId) }
    LaunchedEffect(initial?.key) { initial?.let { selectedId = it.cameraId } }
    var requestedFullscreen by rememberSaveable { mutableStateOf(false) }
    var dismissedLandscapeFullscreen by rememberSaveable { mutableStateOf(false) }
    val selected = cameras.find { it.id == selectedId }
    val landscape = LocalConfiguration.current.orientation == Configuration.ORIENTATION_LANDSCAPE
    LaunchedEffect(landscape) { dismissedLandscapeFullscreen = false }
    val immersive = selected != null && ((landscape && !dismissedLandscapeFullscreen) || requestedFullscreen)
    // Event navigation may focus a camera outside the normal saved set. A preview
    // launch is stricter: the controller never even receives the other cameras.
    SideEffect { controller.cameras(if (singleId != null) viewingCameras else
        viewingCameras + listOfNotNull(selected?.takeIf { it.id !in viewingIds })) }
    LaunchedEffect(selectedId) { controller.focus(selectedId) }
    LaunchedEffect(controller, list, viewingIds) {
        snapshotFlow { list.layoutInfo.let { layout ->
            visibleCameraIds(layout.visibleItemsInfo.mapNotNull { item ->
                (item.key as? Long)?.let { VisibleTile(it, item.offset, item.size) }
            }, layout.viewportStartOffset, layout.viewportEndOffset, true)
        } }.distinctUntilChanged().collect(controller::visible)
    }
    DisposableEffect(controller, lifecycle) {
        controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED))
        val observer = LifecycleEventObserver { _, _ -> controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer); controller.close() }
    }
    LaunchedEffect(immersive) { onImmersive(immersive) }
    DisposableEffect(Unit) { onDispose { onImmersive(false) } }
    val activity = context as? Activity
    DisposableEffect(immersive, activity) {
        val bars = activity?.window?.let { WindowCompat.getInsetsController(it, it.decorView) }
        if (immersive) {
            bars?.systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            bars?.hide(WindowInsetsCompat.Type.systemBars())
        }
        onDispose { bars?.show(WindowInsetsCompat.Type.systemBars()) }
    }
    val closeDetail: () -> Unit = {
        when (liveBackAction(initial, immersive)) {
            LiveBackAction.EXIT_TO_ORIGIN -> onExit()
            LiveBackAction.LEAVE_FULLSCREEN -> { requestedFullscreen = false; dismissedLandscapeFullscreen = true }
            LiveBackAction.SHOW_CAMERA_LIST -> { selectedId = null; requestedFullscreen = false }
        }
    }
    BackHandler(selected != null || initial?.returnToEvents == true) { closeDetail() }
    if (selected == null) {
        Box(Modifier.fillMaxSize()) {
        if (viewingCameras.isEmpty()) Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text(stringResource(if (cameras.isEmpty()) R.string.no_cameras else R.string.live_no_selected_cameras), color = Muted)
        } else LazyColumn(state = list, modifier = Modifier.fillMaxSize().background(PageBackground).testTag("live-camera-list"),
            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(viewingCameras, key = { it.id }) { camera ->
                Box(Modifier.fillMaxWidth().aspectRatio(16f / 9f).clip(RoundedCornerShape(14.dp)).testTag("live-tile-${camera.id}")) {
                    CameraVideo(sessions[camera.id], Modifier.fillMaxSize(), crop = true, onTap = { selectedId = camera.id })
                    Text(camera.name, color=Color.White.copy(alpha=.82f), maxLines=2, overflow=TextOverflow.Ellipsis,
                        style=TextStyle(fontSize=12.sp,shadow=Shadow(Color.Black.copy(alpha=.85f),Offset(0f,1f),3f)),
                        modifier=Modifier.align(Alignment.CenterStart).padding(horizontal=10.dp).widthIn(max=220.dp).testTag("live-name-${camera.id}"))
                }
            }
        }
        if (singleId == null && cameras.isNotEmpty()) CameraSelectionButton(viewingIds.size, cameras.size, { choosing = true },
            Modifier.align(Alignment.TopEnd).padding(top = 16.dp, end = 20.dp).testTag("live-camera-selector"))
        }
    } else {
        key(selected.id) { LiveCameraDetail(selected, capabilities[selected.id], controller, immersive,
            { dismissedLandscapeFullscreen = false; requestedFullscreen = true }, closeDetail, onMessage) }
    }
    if (choosing && singleId == null) LiveCameraPicker(cameras, viewingIds, { choosing = false }) { ids ->
        savedOrder = ids; preferences.save(profileId, ids); choosing = false
    }
}
