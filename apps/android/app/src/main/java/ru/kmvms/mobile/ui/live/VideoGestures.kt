package ru.kmvms.mobile.ui.live

import androidx.compose.foundation.gestures.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import ru.kmvms.mobile.core.fittedFrame
import ru.kmvms.mobile.core.CameraStep
import ru.kmvms.mobile.core.cameraSwipeStep
import kotlin.math.abs

enum class VideoGestureMode { DIGITAL, POINT, AREA }

@Stable class VideoTransform {
    var scale by mutableFloatStateOf(1f)
    var pan by mutableStateOf(Offset.Zero)
    var rectangle by mutableStateOf<Rect?>(null)
    fun reset() { scale = 1f; pan = Offset.Zero; rectangle = null }
}

fun Modifier.videoGestures(transform: VideoTransform, mode: VideoGestureMode, zoomable: Boolean,
                           size: IntSize, aspect: Float, onTap: () -> Unit,
                           point: (Double, Double) -> Unit, area: (Double, Double, Double, Double) -> Unit,
                           onCameraSwipe: ((CameraStep) -> Unit)? = null): Modifier =
    pointerInput(transform, mode, zoomable, size, aspect, onCameraSwipe != null) {
        var lastTap = 0L
        awaitEachGesture {
            val down = awaitFirstDown(requireUnconsumed = false)
            var end = down.position
            var multiple = false
            var moved = false
            var cancelled = false
            var panned = false
            val canSwipe = onCameraSwipe != null && transform.scale <= 1.001f
            var horizontal: Boolean? = null
            try {
                do {
                    val event = awaitPointerEvent()
                    if (event.changes.any { it.isConsumed }) { cancelled = true; break }
                    val pressedCount = event.changes.count { it.pressed }
                    if (pressedCount > 1) {
                        multiple = true; transform.rectangle = null
                        if (zoomable) {
                            val next = (transform.scale * event.calculateZoom()).coerceIn(1f, 8f)
                            val factor = next/transform.scale
                            val anchor = event.calculateCentroid(useCurrent=false) - Offset(size.width/2f,size.height/2f)
                            val pan = transform.pan*factor + anchor*(1-factor) + event.calculatePan()
                            val frame = fittedFrame(size.width.toDouble(), size.height.toDouble(), aspect.toDouble())
                            val xLimit = ((frame.width*next - size.width)/2).coerceAtLeast(0.0).toFloat()
                            val yLimit = ((frame.height*next - size.height)/2).coerceAtLeast(0.0).toFloat()
                            transform.scale = next; transform.pan = Offset(pan.x.coerceIn(-xLimit, xLimit), pan.y.coerceIn(-yLimit, yLimit))
                            event.changes.forEach { it.consume() }
                        }
                    } else if (!multiple) {
                        val finger = event.changes.find { it.id == down.id } ?: break
                        end = finger.position
                        moved = moved || (end-down.position).getDistance() > viewConfiguration.touchSlop
                        if (canSwipe && moved && horizontal == null) {
                            val delta = end - down.position
                            horizontal = abs(delta.x) > abs(delta.y) * 1.5f
                        }
                        if (zoomable && transform.scale > 1.001f && finger.pressed && moved) {
                            panned = true
                            val candidate = transform.pan + finger.position - finger.previousPosition
                            val frame = fittedFrame(size.width.toDouble(), size.height.toDouble(), aspect.toDouble())
                            val xLimit = ((frame.width*transform.scale-size.width)/2).coerceAtLeast(0.0).toFloat()
                            val yLimit = ((frame.height*transform.scale-size.height)/2).coerceAtLeast(0.0).toFloat()
                            transform.pan = Offset(candidate.x.coerceIn(-xLimit, xLimit), candidate.y.coerceIn(-yLimit, yLimit)); finger.consume()
                        } else if (mode == VideoGestureMode.AREA && moved) {
                            if (abs(end.x-down.position.x) >= 12.dp.toPx() && abs(end.y-down.position.y) >= 12.dp.toPx()) {
                                transform.rectangle = Rect(minOf(down.position.x, end.x), minOf(down.position.y, end.y),
                                    maxOf(down.position.x, end.x), maxOf(down.position.y, end.y))
                            } else transform.rectangle = null
                            finger.consume()
                        } else if (canSwipe && horizontal == true) {
                            finger.consume()
                        }
                    }
                } while (event.changes.any { it.pressed })
                if (!multiple && !cancelled) {
                    val step = if (canSwipe && !panned && horizontal == true &&
                        (mode != VideoGestureMode.AREA || abs(end.y-down.position.y) < 12.dp.toPx())) {
                        cameraSwipeStep(end.x-down.position.x, end.y-down.position.y,
                            maxOf(48.dp.toPx(), size.width * .12f))
                    } else null
                    if (step != null) {
                        lastTap = 0L; onCameraSwipe?.invoke(step)
                        return@awaitEachGesture
                    }
                    val frame = fittedFrame(size.width.toDouble(), size.height.toDouble(), aspect.toDouble(),
                        transform.scale.toDouble(), transform.pan.x.toDouble(), transform.pan.y.toDouble())
                    when (mode) {
                        VideoGestureMode.POINT -> if (!moved) frame.point(end.x.toDouble(), end.y.toDouble())?.let { point(it.first, it.second) }
                        VideoGestureMode.AREA -> if (!panned && abs(end.x-down.position.x) >= 12.dp.toPx() && abs(end.y-down.position.y) >= 12.dp.toPx()) {
                            frame.selection(down.position.x.toDouble(), down.position.y.toDouble(), end.x.toDouble(), end.y.toDouble())?.let {
                                area(it.left, it.top, it.right, it.bottom)
                            }
                        }
                        VideoGestureMode.DIGITAL -> {
                            if (!moved) {
                                if (zoomable && down.uptimeMillis - lastTap in 1..300) { transform.reset(); lastTap = 0L }
                                else { lastTap = down.uptimeMillis; onTap() }
                            }
                        }
                    }
                }
            } finally { transform.rectangle = null }
        }
    }
