package ru.kmvms.mobile.ui.live

import android.graphics.Bitmap
import android.view.TextureView
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.IconToggleButton
import androidx.compose.material3.PlainTooltip
import androidx.compose.material3.Text
import androidx.compose.material3.TooltipBox
import androidx.compose.material3.TooltipDefaults
import androidx.compose.material3.rememberTooltipState
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.foundation.layout.size
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import kotlinx.coroutines.delay
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.AiViewStability
import ru.kmvms.mobile.data.LiveAiGeometry
import ru.kmvms.mobile.data.aiViewScore

enum class LiveAiStatus { WAITING, UNCALIBRATED, MISMATCH, PROFILE_UNKNOWN, VISIBLE }

@OptIn(ExperimentalMaterial3Api::class)
@Composable internal fun LiveAiToggle(shown: Boolean, change: () -> Unit, modifier: Modifier = Modifier,
                                      status: LiveAiStatus = LiveAiStatus.WAITING) {
    val description = stringResource(R.string.live_ai_contours)
    val detail = if (shown) when (status) {
        LiveAiStatus.WAITING -> stringResource(R.string.live_ai_waiting)
        LiveAiStatus.UNCALIBRATED -> stringResource(R.string.live_ai_uncalibrated)
        LiveAiStatus.MISMATCH -> stringResource(R.string.live_ai_mismatch)
        LiveAiStatus.PROFILE_UNKNOWN -> stringResource(R.string.live_ai_profile_unknown)
        LiveAiStatus.VISIBLE -> null
    } else null
    TooltipBox(positionProvider = TooltipDefaults.rememberPlainTooltipPositionProvider(),
        tooltip = { PlainTooltip { Text(detail ?: description) } }, state = rememberTooltipState(), modifier = modifier) {
        IconToggleButton(shown, { change() }, Modifier.size(40.dp).testTag("live-ai-toggle")
            .semantics { contentDescription = description; if (detail != null) stateDescription = detail }) {
            Text("AI", color = if (shown) Color(0xFF8EE9F3) else Color.White, fontSize = 12.sp, fontWeight = FontWeight.Bold)
        }
    }
}

internal data class LiveAiFrame(val aspect: Float? = null, val status: LiveAiStatus = LiveAiStatus.WAITING)

@UnstableApi
@Composable internal fun matchingLiveAspect(overlay: LiveAiGeometry?, view: TextureView?, player: ExoPlayer?, ready: Boolean): LiveAiFrame {
    var observed by remember(overlay, view, player, ready) {
        mutableStateOf(LiveAiFrame(status = when {
            overlay != null && overlay.view == null -> LiveAiStatus.UNCALIBRATED
            overlay?.view?.mode == "unknown" -> LiveAiStatus.PROFILE_UNKNOWN
            else -> LiveAiStatus.WAITING
        }))
    }
    LaunchedEffect(overlay, view, player, ready) {
        val reference = overlay?.view ?: return@LaunchedEffect
        if (reference.mode == "unknown" || view == null || player == null || !ready) return@LaunchedEffect
        val fixed = reference.mode == "fixed"
        val edges = if (fixed) null else (reference.bytes() ?: return@LaunchedEffect)
        var state = AiViewStability()
        var prior = Long.MIN_VALUE
        var fresh = 0L
        val bitmap = if (fixed) null else Bitmap.createBitmap(80, 48, Bitmap.Config.ARGB_8888)
        val pixels = IntArray(if (fixed) 0 else 80 * 48)
        val luma = IntArray(if (fixed) 0 else 240)
        try {
            while (true) {
                val position = player.currentPosition
                val now = android.os.SystemClock.elapsedRealtime()
                val video = player.videoSize
                val aspect = if (video.height > 0) video.width * video.pixelWidthHeightRatio / video.height else 0f
                if (!view.isAvailable || !view.isShown || aspect <= 0f || position == prior && now - fresh > 1700) {
                    observed = LiveAiFrame()
                    state = AiViewStability()
                } else if (position != prior) {
                    prior = position; fresh = now
                    val texture = bitmap?.let { runCatching { view.getBitmap(it) }.getOrNull() }
                    if (fixed) {
                        observed = if (reference.geometryMatches(aspect)) LiveAiFrame(aspect, LiveAiStatus.VISIBLE)
                            else LiveAiFrame(status = LiveAiStatus.MISMATCH)
                    } else if (texture == null || edges == null) {
                        observed = LiveAiFrame()
                        state = AiViewStability()
                    } else {
                        texture.getPixels(pixels, 0, 80, 0, 0, 80, 48)
                        for (y in 0..11) for (x in 0..19) {
                            var sum = 0
                            for (dy in 0..3) for (dx in 0..3) {
                                val color = pixels[(4*y+dy)*80 + 4*x+dx]
                                sum += 77 * ((color shr 16) and 255) + 150 * ((color shr 8) and 255) + 29 * (color and 255)
                            }
                            luma[y*20+x] = (sum + 2048) shr 12
                        }
                        state = state.sample(aiViewScore(reference.ratio, edges, luma, aspect))
                        observed = when {
                            state.visible -> LiveAiFrame(aspect, LiveAiStatus.VISIBLE)
                            state.bad >= 2 || observed.status == LiveAiStatus.MISMATCH -> LiveAiFrame(status = LiveAiStatus.MISMATCH)
                            else -> LiveAiFrame()
                        }
                    }
                }
                delay(420)
            }
        } finally {
            bitmap?.recycle()
        }
    }
    return observed
}

internal fun DrawScope.drawLiveAi(overlay: LiveAiGeometry, aspect: Float, crop: Boolean, transform: VideoTransform) {
    val width = if (crop) maxOf(size.width, size.height * aspect) else minOf(size.width, size.height * aspect)
    val height = width / aspect
    val frame = Offset((size.width - width * transform.scale) / 2f, (size.height - height * transform.scale) / 2f) + transform.pan
    for (shape in overlay.shapes) {
        val path = Path().apply {
            shape.points.forEachIndexed { index, pair ->
                val x = frame.x + pair.first * width * transform.scale / 1000f
                val y = frame.y + pair.second * height * transform.scale / 1000f
                if (index == 0) moveTo(x, y) else lineTo(x, y)
            }
            if (!shape.line) close()
        }
        val ink = if (shape.mask) Color(0xFFEDAB76) else Color(0xFF60DFEF)
        if (!shape.line) drawPath(path, ink.copy(alpha = if (shape.mask) .20f else .13f))
        drawPath(path, ink, style = Stroke(1.5.dp.toPx(), pathEffect = if (shape.mask)
            PathEffect.dashPathEffect(floatArrayOf(5.dp.toPx(), 3.dp.toPx())) else null))
    }
}
