package ru.kmvms.mobile.ui.live

import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectDragGesturesAfterLongPress
import androidx.compose.foundation.gestures.scrollBy
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.CustomAccessibilityAction
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.customActions
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.zIndex
import kotlinx.coroutines.launch
import ru.kmvms.mobile.R
import ru.kmvms.mobile.core.LiveCameraOrder
import ru.kmvms.mobile.data.Camera
import ru.kmvms.mobile.ui.Accent
import ru.kmvms.mobile.ui.DashboardImage
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.SoftAccent

@Composable internal fun LiveCameraPicker(cameras: List<Camera>, selected: List<Long>,
                                         dismiss: () -> Unit, apply: (List<Long>) -> Unit) {
    OrderedCameraPicker(cameras, selected, dismiss, apply = apply)
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable internal fun OrderedCameraPicker(cameras: List<Camera>, selected: List<Long>, dismiss: () -> Unit,
    includeRetained: Boolean = false, tagPrefix: String = "live", apply: (List<Long>) -> Unit) {
    var draft by rememberSaveable { mutableStateOf(selected) }
    val available = cameras.map { it.id }
    val ordered = LiveCameraOrder.resolve(draft, available)
    val remaining = cameras.filterNot { it.id in ordered }.sortedBy { it.deleted }
    val rows = ordered.mapNotNull { id -> cameras.find { it.id == id } } + remaining
    val list = rememberLazyListState()
    val scope = rememberCoroutineScope()
    var dragging by remember { mutableStateOf<Long?>(null) }
    var dragCenter by remember { mutableFloatStateOf(0f) }
    val currentOrder by rememberUpdatedState(ordered)
    val moveUp = stringResource(R.string.live_move_up)
    val moveDown = stringResource(R.string.live_move_down)
    val height = (LocalConfiguration.current.screenHeightDp * .72f).dp
    ModalBottomSheet(onDismissRequest = dismiss, containerColor = MaterialTheme.colorScheme.surface,
        modifier = Modifier.testTag("$tagPrefix-camera-picker"), sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.fillMaxWidth().heightIn(max = height).navigationBarsPadding()) {
            Row(Modifier.fillMaxWidth().padding(start = 20.dp, end = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(stringResource(R.string.live_choose_cameras), style = MaterialTheme.typography.titleMedium,
                    modifier = Modifier.weight(1f))
                Text("${ordered.size}/${cameras.size}", color = Muted, style = MaterialTheme.typography.labelMedium)
                IconButton(onClick = dismiss, modifier = Modifier.testTag("$tagPrefix-picker-close")) {
                    Icon(Icons.Default.Close, stringResource(R.string.close))
                }
            }
            LazyColumn(state = list, modifier = Modifier.weight(1f, fill = false).testTag("$tagPrefix-picker-list"),
                contentPadding = PaddingValues(horizontal = 12.dp)) {
                items(rows, key = { it.id }) { camera ->
                    val index = ordered.indexOf(camera.id)
                    val chosen = index >= 0
                    val dragged = dragging == camera.id
                    val currentItem = list.layoutInfo.visibleItemsInfo.find { it.key == camera.id }
                    if (includeRetained && camera == remaining.firstOrNull { it.deleted }) {
                        HorizontalDivider(Modifier.padding(vertical = 6.dp))
                        Text(stringResource(R.string.retained_archive_cameras), color = Muted,
                            style = MaterialTheme.typography.labelSmall,
                            modifier = Modifier.padding(start = 12.dp, bottom = 4.dp).testTag("timeline-retained-divider"))
                    }
                    Row(Modifier.fillMaxWidth().height(56.dp).zIndex(if (dragged) 1f else 0f)
                        .graphicsLayer { translationY = if (dragged && currentItem != null)
                            dragCenter - currentItem.offset - currentItem.size / 2f else 0f }
                        .background(if (dragged) SoftAccent else MaterialTheme.colorScheme.surface),
                        verticalAlignment = Alignment.CenterVertically) {
                        Row(Modifier.weight(1f).fillMaxHeight().testTag("$tagPrefix-picker-camera-${camera.id}")
                            .toggleable(chosen, role = Role.Checkbox, onValueChange = { draft = LiveCameraOrder.toggle(currentOrder, camera.id) }),
                            verticalAlignment = Alignment.CenterVertically) {
                            Checkbox(chosen, onCheckedChange = null, modifier = Modifier.padding(horizontal = 8.dp))
                            Box(Modifier.size(26.dp).background(if (chosen) SoftAccent else MaterialTheme.colorScheme.surface, CircleShape)
                                .testTag("$tagPrefix-picker-order-${camera.id}"), contentAlignment = Alignment.Center) {
                                if (chosen) Text("${index + 1}", color = Accent, style = MaterialTheme.typography.labelMedium,
                                    fontWeight = FontWeight.SemiBold)
                            }
                            Text(camera.name, maxLines = 1, overflow = TextOverflow.Ellipsis,
                                modifier = Modifier.weight(1f).padding(start = 10.dp))
                            if (includeRetained && camera.deleted) Text(" (${stringResource(R.string.deleted_camera)})",
                                color = Muted, style = MaterialTheme.typography.labelSmall)
                        }
                        val drag = if (!chosen) Modifier else Modifier.pointerInput(camera.id) {
                            detectDragGesturesAfterLongPress(
                                onDragStart = {
                                    list.layoutInfo.visibleItemsInfo.find { it.key == camera.id }?.let {
                                        dragging = camera.id; dragCenter = it.offset + it.size / 2f
                                    }
                                },
                                onDragEnd = { dragging = null }, onDragCancel = { dragging = null }
                            ) { change, amount ->
                                change.consume()
                                dragCenter += amount.y
                                val target = list.layoutInfo.visibleItemsInfo.find {
                                    dragCenter >= it.offset && dragCenter < it.offset + it.size
                                }?.key as? Long
                                val destination = currentOrder.indexOf(target)
                                if (destination >= 0) draft = LiveCameraOrder.move(currentOrder, camera.id, destination)
                                val layout = list.layoutInfo
                                if (dragCenter < layout.viewportStartOffset + 40 || dragCenter > layout.viewportEndOffset - 40)
                                    scope.launch { list.scrollBy(amount.y) }
                            }
                        }
                        Box(Modifier.size(48.dp).then(drag).testTag("$tagPrefix-picker-drag-${camera.id}")
                            .semantics {
                                customActions = if (!chosen) emptyList() else listOf(
                                    CustomAccessibilityAction(moveUp) { draft = LiveCameraOrder.move(currentOrder, camera.id, index - 1); true },
                                    CustomAccessibilityAction(moveDown) { draft = LiveCameraOrder.move(currentOrder, camera.id, index + 1); true })
                            }, contentAlignment = Alignment.Center) {
                            if (chosen) Icon(Icons.Default.Menu, stringResource(R.string.live_reorder_camera), tint = Muted,
                                modifier = Modifier.size(20.dp))
                        }
                    }
                }
            }
            HorizontalDivider(color = MaterialTheme.colorScheme.outline)
            Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TextButton(onClick = { draft = if (includeRetained) cameras.filterNot { it.deleted }.map { it.id } else available }, modifier = Modifier.testTag("$tagPrefix-picker-all")) { Text(stringResource(R.string.all_cameras)) }
                TextButton(onClick = { draft = emptyList() }, modifier = Modifier.testTag("$tagPrefix-picker-clear")) { Text(stringResource(R.string.live_clear_selection)) }
                Spacer(Modifier.weight(1f))
                FilledTonalButton(onClick = { apply(ordered) }, modifier = Modifier.testTag("$tagPrefix-picker-apply")) {
                    Text(stringResource(R.string.live_apply_selection))
                }
            }
        }
    }
}

@Composable internal fun CameraSelectionButton(count: Int, total: Int, onClick: () -> Unit, modifier: Modifier = Modifier) {
    val label = stringResource(R.string.live_choose_cameras)
    FilledTonalButton(onClick = onClick, modifier = modifier.height(44.dp).semantics { contentDescription = label },
        contentPadding = PaddingValues(horizontal = 10.dp), shape = androidx.compose.foundation.shape.RoundedCornerShape(12.dp)) {
        DashboardImage(R.drawable.kmvms_camera, Modifier.size(24.dp))
        Spacer(Modifier.width(6.dp))
        Text("$count/$total", style = MaterialTheme.typography.labelMedium)
    }
}
