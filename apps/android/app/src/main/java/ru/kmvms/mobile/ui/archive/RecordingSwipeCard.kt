package ru.kmvms.mobile.ui.archive

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.snap
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.Orientation
import androidx.compose.foundation.gestures.draggable
import androidx.compose.foundation.gestures.rememberDraggableState
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.CustomAccessibilityAction
import androidx.compose.ui.semantics.customActions
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import ru.kmvms.mobile.R
import kotlin.math.roundToInt

/** Swipe reveals an action, never deletes. Actual removal still needs confirmation. */
@Composable internal fun RecordingSwipeCard(identity: String, canDelete: Boolean, deleting: Boolean,
    requestDelete: () -> Unit, content: @Composable () -> Unit) {
    var revealed by rememberSaveable(identity) { mutableStateOf(false) }
    var dragging by remember(identity) { mutableStateOf(false) }
    var dragged by remember(identity) { mutableFloatStateOf(0f) }
    val width = with(LocalDensity.current) { 64.dp.toPx() }
    val offset by animateFloatAsState(if (!canDelete) 0f else if (dragging) dragged else if (revealed) -width else 0f,
        animationSpec = if (dragging) snap() else tween(160), label = "recording-delete-reveal")
    val label = stringResource(R.string.remove)
    val gesture = if (!canDelete || deleting) Modifier else Modifier.draggable(
        state = rememberDraggableState { dragged = (dragged + it).coerceIn(-width, 0f) }, orientation = Orientation.Horizontal,
        onDragStarted = { dragged = if (revealed) -width else 0f; dragging = true },
        onDragStopped = { velocity -> revealed = if (velocity < -800) true else if (velocity > 800) false else dragged < -width / 2; dragging = false })
        .semantics { customActions = listOf(CustomAccessibilityAction(label) { revealed = true; true }) }
    Box(Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).testTag("recording-swipe-$identity")) {
        if (canDelete && (revealed || dragging)) Box(Modifier.matchParentSize().background(Color(0xFFFCECEF)), contentAlignment = Alignment.CenterEnd) {
            IconButton(onClick = requestDelete, enabled = revealed && !dragging && !deleting,
                modifier = Modifier.width(64.dp).fillMaxHeight().testTag("recording-delete-$identity")) {
                Icon(painterResource(R.drawable.ic_camera_trash), label, tint = Color(0xFFD85C63), modifier = Modifier.size(21.dp))
            }
        }
        Box(Modifier.offset { IntOffset(offset.roundToInt(), 0) }.then(gesture)) { content() }
    }
}
