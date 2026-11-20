package ru.kmvms.mobile.ui.recording

import android.app.TimePickerDialog
import androidx.annotation.StringRes
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.NasException
import ru.kmvms.mobile.events.CameraEventKind
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.camera.LabeledChoice
import ru.kmvms.mobile.ui.events.label
import java.time.DayOfWeek
import java.time.format.TextStyle
import java.util.Locale
import kotlin.math.roundToInt

@StringRes
private fun ruleLabel(kind: String) = when (kind) {
    "enter" -> R.string.ai_rule_kind_enter
    "exit" -> R.string.ai_rule_kind_exit
    "crossing" -> R.string.ai_rule_kind_crossing
    "dwell" -> R.string.ai_rule_kind_dwell
    "scene" -> R.string.ai_rule_kind_scene
    else -> R.string.ai_rule_kind_presence
}

@StringRes
private fun directionLabel(value: String) = when (value) {
    "forward" -> R.string.ai_rule_direction_forward
    "reverse" -> R.string.ai_rule_direction_reverse
    else -> R.string.ai_rule_direction_both
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
internal fun AiRuleEditor(
    camera: RecordingCamera, draft: RecordingDraft, service: RecordingCenterApi,
    onChange: (AiRules?) -> Unit, close: () -> Unit,
) {
    val value = draft.aiRules ?: AiRules()
    var selected by remember(camera.id) { mutableStateOf(value.rules.firstOrNull()?.id.orEmpty()) }
    var mask by remember(camera.id) { mutableStateOf<Int?>(null) }
    var frame by remember(camera.id) { mutableStateOf<AiEditorFrame?>(null) }
    var pending by remember { mutableStateOf(false) }
    var captureError by remember { mutableStateOf<Int?>(null) }
    var drawing by remember { mutableStateOf(false) }
    var tuning by remember(selected) { mutableStateOf(false) }
    var scheduleOpen by remember(selected) { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val rule = value.rules.firstOrNull { it.id == selected }
    val maskId = mask
    val points = if (maskId != null) value.exclusions.getOrNull(maskId).orEmpty() else rule?.points.orEmpty()
    val line = maskId == null && rule?.kind == "crossing"
    val canDraw = drawing && (maskId != null || rule != null && rule.kind != "scene")

    fun patch(transform: (AiRule) -> AiRule) {
        onChange(value.copy(rules = value.rules.map { if (it.id == selected) transform(it) else it }))
    }
    fun setPoints(next: List<AiPoint>) {
        val reference = frame?.reference ?: return
        if (maskId != null) onChange(value.copy(reference = reference,
            exclusions = value.exclusions.mapIndexed { i, item -> if (i == maskId) next else item }))
        else onChange(value.copy(reference = reference,
            rules = value.rules.map { if (it.id == selected) it.copy(points = next) else it }))
    }
    fun capture() {
        if (pending) return
        pending = true
        captureError = null
        scope.launch {
            try { frame = service.ruleFrame(camera.id, if (draft.mode == "continuous") "none" else draft.profile) }
            catch (error: CancellationException) { throw error }
            catch (error: Exception) {
                captureError = if ((error as? NasException)?.publicReason == "ai_frame_busy")
                    R.string.ai_rule_frame_busy else R.string.ai_rule_frame_error
            } finally { pending = false }
        }
    }
    Dialog(onDismissRequest = close, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(modifier = Modifier.padding(12.dp).widthIn(max = 720.dp).fillMaxWidth().testTag("ai-rule-editor"),
            shape = RoundedCornerShape(14.dp), color = Color.White) {
            Column(Modifier.padding(horizontal = 14.dp).padding(top = 14.dp, bottom = 4.dp)) {
                Text(stringResource(R.string.ai_rule_edit) + " · " + camera.name,
                    style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(bottom = 8.dp))
                Column(Modifier.weight(1f, fill = false).verticalScroll(rememberScrollState()),
                    verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Text(stringResource(R.string.ai_rule_picture), Modifier.weight(1f), style = MaterialTheme.typography.labelLarge)
                        TextButton(onClick = ::capture, enabled = !pending, modifier = Modifier.testTag("ai-rule-capture")) {
                            Text(stringResource(if (pending) R.string.ai_rule_frame_loading
                            else if (frame == null) R.string.ai_rule_capture else R.string.ai_rule_frame_refresh))
                        }
                    }
                    val reference = frame
                    if (reference != null) {
                        RuleImage(reference, value, selected, maskId, canDraw, points) { point ->
                            if (points.size < if (line) 2 else 10) setPoints(points + point)
                        }
                        if (!reference.matchingSaved) Text(stringResource(R.string.ai_rule_frame_replaced),
                            color = Color(0xFF805018), fontSize = 12.sp)
                    } else Box(Modifier.fillMaxWidth().heightIn(min = 96.dp).background(Color(0xFFEAF2FA), RoundedCornerShape(8.dp))
                        .padding(12.dp), contentAlignment = Alignment.Center) {
                        Text(stringResource(R.string.ai_rule_frame_hint), color = Muted, fontSize = 13.sp)
                    }
                    captureError?.let { Text(stringResource(it), color = MaterialTheme.colorScheme.error, fontSize = 12.sp) }
                    if (reference != null && (maskId != null || rule != null && rule.kind != "scene")) {
                        FlowRow(horizontalArrangement = Arrangement.spacedBy(3.dp)) {
                            TextButton(onClick = { drawing = !drawing }, modifier = Modifier.testTag("ai-rule-draw")) {
                                Text(stringResource(if (drawing) R.string.ai_rule_finish_draw else R.string.ai_rule_draw))
                            }
                            TextButton(onClick = { setPoints(points.dropLast(1)) }, enabled = points.isNotEmpty()) { Text(stringResource(R.string.ai_rule_undo)) }
                            TextButton(onClick = { setPoints(emptyList()); drawing = true }, enabled = points.isNotEmpty()) { Text(stringResource(R.string.ai_rule_clear)) }
                        }
                        if (canDraw) Text(stringResource(if (line) R.string.ai_rule_line_hint else R.string.ai_rule_draw_help), color = Muted, fontSize = 12.sp)
                    }
                    FlowRow(horizontalArrangement = Arrangement.spacedBy(5.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                        for (i in value.exclusions.indices) FilterChip(maskId == i, onClick = { mask = if (maskId == i) null else i; drawing = false },
                            label = { Text(stringResource(R.string.ai_rule_mask_n, i+1)) })
                        TextButton(enabled = reference != null && value.rules.isNotEmpty() && value.exclusions.size < 4, onClick = {
                            mask = value.exclusions.size
                            drawing = true
                            onChange(value.copy(reference = reference?.reference, exclusions = value.exclusions + listOf(emptyList())))
                        }) { Text(stringResource(R.string.ai_rule_add_mask)) }
                        if (maskId != null) TextButton(enabled = reference != null, onClick = {
                            onChange(value.copy(reference = reference?.reference ?: value.reference, exclusions = value.exclusions.filterIndexed { i, _ -> i != maskId }))
                            mask = null; drawing = false
                        }) { Text(stringResource(R.string.ai_rule_remove_mask)) }
                    }
                    Text(stringResource(R.string.ai_rule_exclusion_note), color = Muted, fontSize = 12.sp)
                    HorizontalDivider(color = Color(0xFFDCE7F1))
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Text(stringResource(R.string.ai_rule_items), Modifier.weight(1f), style = MaterialTheme.typography.labelLarge)
                        TextButton(enabled = value.rules.size < 8, modifier = Modifier.testTag("ai-rule-add"), onClick = {
                            val added = AiRule()
                            selected = added.id; mask = null; drawing = false
                            onChange(value.copy(rules = value.rules + added))
                        }) { Text(stringResource(R.string.ai_rule_add)) }
                    }
                    if (value.rules.size > 1 || maskId != null) LabeledChoice(stringResource(R.string.ai_rule_items),
                        rule?.name?.takeIf(String::isNotBlank) ?: rule?.let { stringResource(ruleLabel(it.kind)) }.orEmpty(),
                        value.rules.map { it.id to it.name.ifBlank { stringResource(ruleLabel(it.kind)) } }, true, "ai-rule-selected") {
                        selected = it; mask = null; drawing = false
                    }
                    if (maskId != null) Text(stringResource(R.string.ai_rule_mask_edit), color = Muted, fontSize = 12.sp)
                    else if (rule != null) {
                        OutlinedTextField(rule.name, { if (it.length <= 48) patch { old -> old.copy(name = it) } },
                            label = { Text(stringResource(R.string.ai_rule_name)) }, singleLine = true, modifier = Modifier.fillMaxWidth())
                        LabeledChoice(stringResource(R.string.ai_rule_type), stringResource(ruleLabel(rule.kind)),
                            aiRuleKinds.map { it to stringResource(ruleLabel(it)) }, true, "ai-rule-kind") { kind ->
                            patch { it.copy(kind = kind, points = emptyList(), direction = "both",
                                sceneObjects = kind == "scene" && it.sceneObjects) }; drawing = false
                        }
                        if (rule.kind == "scene") {
                            RuleToggle(R.string.ai_rule_scene_objects, rule.sceneObjects) { on ->
                                patch { it.copy(sceneObjects = on) }
                            }
                        }
                        if (rule.kind != "scene" || rule.sceneObjects) {
                            FlowRow(horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                                CameraEventKind.entries.filter { it.code in aiRuleClasses }.forEach { entry ->
                                    FilterChip(entry.code in rule.classes, onClick = {
                                        patch { it.copy(classes = if (entry.code in it.classes) it.classes - entry.code else it.classes + entry.code) }
                                    }, label = { Text(stringResource(entry.label())) })
                                }
                            }
                            if (line) LabeledChoice(stringResource(R.string.ai_rule_direction), stringResource(directionLabel(rule.direction)),
                                listOf("both", "forward", "reverse").map { it to stringResource(directionLabel(it)) }, true, "ai-rule-direction") { direction ->
                                patch { it.copy(direction = direction) }
                            }
                            TextButton(onClick = { tuning = !tuning }) { Text(stringResource(R.string.ai_rule_tuning)) }
                            if (tuning || rule.kind == "dwell") {
                                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                    NumberValue(R.string.ai_rule_confidence, rule.confidence, Modifier.weight(1f)) { n -> patch { it.copy(confidence = n) } }
                                    NumberValue(R.string.ai_rule_confirm, rule.confirm, Modifier.weight(1f)) { n -> patch { it.copy(confirm = n) } }
                                }
                                if (!line) NumberValue(R.string.ai_rule_release, rule.release, Modifier.fillMaxWidth()) { n -> patch { it.copy(release = n) } }
                                if (rule.kind == "dwell") NumberValue(R.string.ai_rule_dwell, rule.dwell, Modifier.fillMaxWidth()) { n -> patch { it.copy(dwell = n) } }
                            }
                        }
                        TextButton(onClick = { scheduleOpen = !scheduleOpen }, modifier = Modifier.testTag("ai-rule-schedule")) { Text(stringResource(R.string.ai_rule_schedule)) }
                        val schedule = rule.schedule
                        if (scheduleOpen) {
                            RuleToggle(R.string.ai_rule_custom_time, schedule != null) { on -> patch { it.copy(schedule = AiSchedule().takeIf { on }) } }
                            if (schedule != null) {
                                FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                                    for (day in 1..7) FilterChip(day in schedule.days, onClick = { patch {
                                        it.copy(schedule = schedule.copy(days = if (day in schedule.days) schedule.days-day else schedule.days+day)) } },
                                        label = { Text(DayOfWeek.of(day).getDisplayName(TextStyle.SHORT, Locale.getDefault())) })
                                }
                                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                    RuleTime(R.string.ai_rule_start, schedule.start) { n -> patch { it.copy(schedule = schedule.copy(start = n)) } }
                                    RuleTime(R.string.ai_rule_end, schedule.end) { n -> patch { it.copy(schedule = schedule.copy(end = n)) } }
                                }
                                Text(stringResource(R.string.ai_rule_local_time), color = Muted, fontSize = 12.sp)
                            }
                        }
                        TextButton(onClick = {
                            val rest = value.rules.filterNot { it.id == selected }
                            selected = rest.firstOrNull()?.id.orEmpty(); drawing = false
                            onChange(value.copy(rules = rest).takeIf { rest.isNotEmpty() })
                        }) { Text(stringResource(R.string.ai_rule_remove)) }
                    } else Text(stringResource(R.string.ai_rule_default), color = Muted, fontSize = 12.sp)
                    draft.aiRules?.issue()?.let { Text(stringResource(it), color = MaterialTheme.colorScheme.error, fontSize = 12.sp) }
                }
                TextButton(onClick = close, modifier = Modifier.align(Alignment.End).testTag("ai-rule-done")) { Text(stringResource(R.string.ai_rule_done)) }
            }
        }
    }
}

@Composable private fun RuleToggle(@StringRes label: Int, checked: Boolean, change: (Boolean) -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        val text = stringResource(label)
        Checkbox(checked, change, modifier = Modifier.size(42.dp))
        Text(text, fontSize = 13.sp)
    }
}

@Composable private fun NumberValue(@StringRes label: Int, value: Int, modifier: Modifier, change: (Int) -> Unit) {
    var typed by remember(value) { mutableStateOf(value.toString()) }
    OutlinedTextField(typed, { next -> if (next.length <= 3 && next.all { it in '0'..'9' }) {
        typed = next; next.toIntOrNull()?.let(change)
    } }, singleLine = true, label = { Text(stringResource(label), fontSize = 12.sp) },
        modifier = modifier, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number))
}

@Composable private fun RuleTime(@StringRes label: Int, value: Int, change: (Int) -> Unit) {
    val context = LocalContext.current
    OutlinedButton(onClick = {
        TimePickerDialog(context, { _, hour, minute -> change(hour*60+minute) }, value/60, value%60, true).show()
    }) { Text(stringResource(label) + "  " + String.format(Locale.ROOT, "%02d:%02d", value/60, value%60)) }
}

@Composable private fun RuleImage(
    frame: AiEditorFrame, config: AiRules, selected: String, mask: Int?, drawing: Boolean,
    points: List<AiPoint>, onTap: (AiPoint) -> Unit,
) {
    val bitmap = remember(frame) { frame.image.asImageBitmap() }
    val click = rememberUpdatedState(onTap)
    Box(Modifier.fillMaxWidth().aspectRatio(bitmap.width.toFloat()/bitmap.height).clip(RoundedCornerShape(8.dp))
        .background(Color(0xFF162B45)).testTag("ai-rule-frame")) {
        Image(bitmap = bitmap, contentDescription = stringResource(R.string.ai_rule_picture),
            modifier = Modifier.matchParentSize(), contentScale = ContentScale.FillBounds)
        Canvas(Modifier.matchParentSize().pointerInput(drawing, points) {
            if (drawing) detectTapGestures { at -> click.value(AiPoint((at.x*1000/size.width).roundToInt().coerceIn(0,1000),
                (at.y*1000/size.height).roundToInt().coerceIn(0,1000))) }
        }) {
            fun loc(p: AiPoint) = Offset(p.x*size.width/1000, p.y*size.height/1000)
            fun paint(items: List<AiPoint>, current: Boolean, exclusion: Boolean, direction: String?) {
                if (items.isEmpty()) return
                val ink = if (exclusion) Color(0xFFFFC69A) else Color(if (current) 0xFF69EFFF else 0xFFAAE4ED)
                val vertices = items.map(::loc)
                val path = Path().apply {
                    moveTo(vertices[0].x,vertices[0].y)
                    vertices.drop(1).forEach { lineTo(it.x,it.y) }
                    if (direction == null && vertices.size >= 3) close()
                }
                if (direction == null && vertices.size >= 3) drawPath(path, ink.copy(alpha = if (exclusion) .42f else .19f))
                drawPath(path, ink, style = Stroke(2.dp.toPx()))
                if (current) vertices.forEach { drawCircle(ink, 3.dp.toPx(), it) }
                if (direction != null && vertices.size == 2) {
                    val (a, b) = vertices
                    val mid = (a+b)*.5f
                    val axis = (b-a) / (b-a).getDistance().coerceAtLeast(1f) * 15.dp.toPx()
                    val normal = Offset(-axis.y, axis.x)
                    for (i in listOf(1, -1)) {
                        if ((i == 1 && direction == "reverse") || (i == -1 && direction == "forward")) continue
                        val head = mid + normal*i.toFloat()
                        val tail = mid + normal*(.55f*i)
                        drawLine(ink, head, tail+axis*.42f, 2.dp.toPx())
                        drawLine(ink, head, tail-axis*.42f, 2.dp.toPx())
                    }
                }
            }
            config.rules.forEach { paint(it.points, it.id == selected && mask == null, false, it.direction.takeIf { _ -> it.kind == "crossing" }) }
            config.exclusions.forEachIndexed { i, poly -> paint(poly, i == mask, true, null) }
        }
    }
}
