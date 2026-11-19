package ru.kmvms.mobile.ui.live

import android.view.LayoutInflater
import android.view.TextureView
import androidx.compose.foundation.background
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.foundation.layout.*
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.media3.common.util.UnstableApi
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import androidx.media3.exoplayer.ExoPlayer
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import ru.kmvms.mobile.R
import ru.kmvms.mobile.core.CameraStep
import ru.kmvms.mobile.media.LivePlaybackSession
import ru.kmvms.mobile.media.VideoState
import ru.kmvms.mobile.media.VideoStatus
import ru.kmvms.mobile.data.LiveAiGeometry

@UnstableApi
@Composable fun CameraVideo(session: LivePlaybackSession?, modifier: Modifier = Modifier,
                           crop: Boolean = false, zoomable: Boolean = false, onTap: (() -> Unit)? = null,
                           aspect: Float? = null, mode: VideoGestureMode = VideoGestureMode.DIGITAL,
                           onPoint: (Double, Double) -> Unit = { _, _ -> },
                           onArea: (Double, Double, Double, Double) -> Unit = { _, _, _, _ -> },
                           onCameraSwipe: ((CameraStep) -> Unit)? = null, aiOverlay: LiveAiGeometry? = null,
                           onAiStatus: ((LiveAiStatus) -> Unit)? = null) {
    val observed = session?.state?.collectAsStateWithLifecycle()
    val state = observed?.value ?: VideoState()
    VideoSurface(session?.player, state, { session?.retry() }, modifier, crop, zoomable, onTap, aspect, mode, onPoint, onArea, onCameraSwipe, aiOverlay, onAiStatus)
}

@UnstableApi
@Composable fun VideoSurface(player: ExoPlayer?, state: VideoState, retry: () -> Unit,
                             modifier: Modifier = Modifier, crop: Boolean = false, zoomable: Boolean = false,
                             onTap: (() -> Unit)? = null, aspect: Float? = null, mode: VideoGestureMode = VideoGestureMode.DIGITAL,
                             onPoint: (Double, Double) -> Unit = { _, _ -> }, onArea: (Double, Double, Double, Double) -> Unit = { _, _, _, _ -> },
                             onCameraSwipe: ((CameraStep) -> Unit)? = null, aiOverlay: LiveAiGeometry? = null,
                             onAiStatus: ((LiveAiStatus) -> Unit)? = null) {
    val transform = remember(player) { VideoTransform() }
    var size by remember { mutableStateOf(IntSize.Zero) }
    val tap by rememberUpdatedState(onTap)
    val point by rememberUpdatedState(onPoint)
    val area by rememberUpdatedState(onArea)
    val swipe by rememberUpdatedState(onCameraSwipe)
    val videoSize = player?.videoSize
    var texture by remember { mutableStateOf<TextureView?>(null) }
    val aiFrame = matchingLiveAspect(aiOverlay, texture, player, state.status == VideoStatus.PLAYING)
    val confirmedAspect = aiFrame.aspect
    val aiListener by rememberUpdatedState(onAiStatus)
    LaunchedEffect(aiFrame.status, aiOverlay) { aiListener?.invoke(aiFrame.status) }
    val sourceAspect = aspect ?: if (videoSize != null && videoSize.height > 0) videoSize.width.toFloat()/videoSize.height else 16f/9f
    val zoomDescription = stringResource(R.string.digital_zoom_level, transform.scale)
    Box(modifier.background(Color.Black).clipToBounds().onSizeChanged { size = it }, contentAlignment = Alignment.Center) {
        if (player != null) AndroidView(factory = { context ->
            (LayoutInflater.from(context).inflate(R.layout.view_camera_player, android.widget.FrameLayout(context), false) as PlayerView).apply {
                useController = false; isClickable = false; isFocusable = false
            }
        }, modifier = Modifier.fillMaxSize().graphicsLayer { scaleX = transform.scale; scaleY = transform.scale; translationX = transform.pan.x; translationY = transform.pan.y },
            update = { view -> view.resizeMode = if (crop) AspectRatioFrameLayout.RESIZE_MODE_ZOOM else AspectRatioFrameLayout.RESIZE_MODE_FIT
                if (view.player !== player) view.player = player
                texture = view.videoSurfaceView as? TextureView },
            onRelease = { if (texture === it.videoSurfaceView) texture = null; it.player = null })
        Canvas(Modifier.fillMaxSize().testTag("video-gesture-surface").semantics { stateDescription = zoomDescription }
            .videoGestures(transform, mode, zoomable, size, sourceAspect, { tap?.invoke() },
            { x, y -> point(x, y) }, { left, top, right, bottom -> area(left, top, right, bottom) },
                if (onCameraSwipe != null) { step -> swipe?.invoke(step) } else null)) {
            if (aiOverlay != null && confirmedAspect != null) drawLiveAi(aiOverlay, confirmedAspect, crop, transform)
            transform.rectangle?.let { selection ->
                drawRect(Color(0x302563EB), selection.topLeft, selection.size)
                drawRect(Color(0xFF60A5FA), selection.topLeft, selection.size, style = Stroke(2.dp.toPx()))
            }
        }
        when (state.status) {
            VideoStatus.FAILED -> Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.testTag("video-failed")) {
                Text(stringResource(if (state.accessDenied) R.string.video_access_expired else if (state.unsupportedCodec) R.string.video_codec_unsupported else R.string.video_unavailable), color = Color.White)
                TextButton(onClick = retry) { Text(stringResource(R.string.retry), color = Color.White) }
            }
            VideoStatus.PLAYING -> Box(Modifier.size(1.dp).testTag("video-playing"))
            else -> CircularProgressIndicator(Modifier.size(28.dp).testTag(if (player == null) "video-inactive" else "video-loading"), color = Color.White, strokeWidth = 2.dp)
        }
    }
}
