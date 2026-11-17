package ru.kmvms.mobile.ui.live

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.onClick
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import ru.kmvms.mobile.R
import ru.kmvms.mobile.ptz.PtzHandle
import ru.kmvms.mobile.ptz.PtzPreset
import ru.kmvms.mobile.ui.Accent
import ru.kmvms.mobile.ui.SoftAccent
import kotlin.math.roundToInt

@Composable fun PtzControls(handle: PtzHandle, mode: VideoGestureMode, setMode: (VideoGestureMode) -> Unit,
                            modifier: Modifier = Modifier, overlay: Boolean = false,
                            headerDrag: Modifier = Modifier, collapse: (() -> Unit)? = null, ready: Boolean = true) {
    val held by handle.held.collectAsStateWithLifecycle()
    val busy by handle.busy.collectAsStateWithLifecycle()
    val presets by handle.presets.collectAsStateWithLifecycle()
    var speed by remember { mutableFloatStateOf(2f) }
    val ink = if (overlay) Color.White else MaterialTheme.colorScheme.onSurface
    val panel = if (overlay) Color(0xBA233044) else Color.White
    Column(modifier.background(panel, RoundedCornerShape(16.dp)).padding(12.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(8.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Row(Modifier.fillMaxWidth().then(headerDrag), verticalAlignment = Alignment.CenterVertically) {
            Text("PTZ", color = ink, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
            if (overlay && presets.isNotEmpty()) PresetPicker(presets, !busy && ready, ink, compact = true, choose = handle::preset)
            if (handle.capability.point) IconButton(onClick = { handle.stop(); setMode(if (mode == VideoGestureMode.POINT) VideoGestureMode.DIGITAL else VideoGestureMode.POINT) },
                modifier = Modifier.size(40.dp).testTag("ptz-point")) {
                PlayerGlyph(PlayerGlyph.POINT, if (mode == VideoGestureMode.POINT) Color(0xFF60A5FA) else ink, stringResource(R.string.ptz_point))
            }
            if (handle.capability.area) IconButton(onClick = { handle.stop(); setMode(if (mode == VideoGestureMode.AREA) VideoGestureMode.DIGITAL else VideoGestureMode.AREA) },
                modifier = Modifier.size(40.dp).testTag("ptz-area")) {
                PlayerGlyph(PlayerGlyph.AREA, if (mode == VideoGestureMode.AREA) Color(0xFF60A5FA) else ink, stringResource(R.string.ptz_area))
            }
            if (collapse != null) IconButton(onClick = { handle.stop(); collapse() }, modifier = Modifier.size(40.dp)) {
                Icon(Icons.Default.Close, stringResource(R.string.ptz_collapse), tint = ink)
            }
        }
        if (handle.capability.panTilt) {
            val directions = listOf("up_left", "up", "up_right", "left", "stop", "right", "down_left", "down", "down_right")
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                directions.chunked(3).forEach { row -> Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    row.forEach { direction -> if (direction == "stop") {
                        FilledTonalIconButton(onClick = { handle.stop(true) }, Modifier.size(44.dp).testTag("ptz-stop")) {
                            Text("■", color = Accent)
                        }
                    } else MovementButton(handle, "move", direction, speed, held == direction, !busy && ready, ink) }
                } }
            }
        }
        if (handle.capability.zoom) Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceEvenly) {
            MovementButton(handle, "zoom", "out", speed, held == "out", !busy && ready, ink)
            Text(stringResource(R.string.ptz_zoom), color = ink)
            MovementButton(handle, "zoom", "in", speed, held == "in", !busy && ready, ink)
        }
        if (handle.capability.panTilt || handle.capability.zoom) Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(stringResource(R.string.ptz_speed), color = ink, modifier = Modifier.padding(end = 12.dp))
            Slider(speed, { speed = it.roundToInt().toFloat() }, valueRange = 1f..5f, steps = 3,
                modifier = Modifier.weight(1f).testTag("ptz-speed"))
        }
        if (!overlay && presets.isNotEmpty()) PresetPicker(presets, !busy && ready, ink, compact = false, choose = handle::preset)
    }
}

@Composable internal fun PresetPicker(presets: List<PtzPreset>, enabled: Boolean, ink: Color, compact: Boolean, choose: (String) -> Unit) {
    var menu by remember { mutableStateOf(false) }
    Box(if (compact) Modifier else Modifier.fillMaxWidth()) {
        if (compact) IconButton(onClick = { menu = true }, enabled = enabled, modifier = Modifier.size(40.dp).testTag("ptz-preset-picker")) {
            PlayerGlyph(PlayerGlyph.PRESETS, ink.copy(alpha = if (enabled) 1f else .4f), stringResource(R.string.ptz_saved_position))
        } else OutlinedButton(onClick = { menu = true }, modifier = Modifier.fillMaxWidth().testTag("ptz-presets"), enabled = enabled) {
            Text(stringResource(R.string.ptz_saved_position), color = ink)
        }
        DropdownMenu(menu && enabled, { menu = false }, modifier = Modifier.heightIn(max = 280.dp).testTag("ptz-preset-menu")) {
            presets.forEachIndexed { index, preset ->
                DropdownMenuItem(text = { Text(preset.name.ifBlank { stringResource(R.string.ptz_position, index + 1) }) },
                    onClick = { menu = false; choose(preset.token) })
            }
        }
    }
}

@Composable private fun MovementButton(handle: PtzHandle, action: String, direction: String, level: Float,
                                       selected: Boolean, enabled: Boolean, ink: Color) {
    val label = stringResource(when (direction) {
        "up_left" -> R.string.ptz_up_left; "up" -> R.string.ptz_up; "up_right" -> R.string.ptz_up_right
        "left" -> R.string.ptz_left; "right" -> R.string.ptz_right; "down_left" -> R.string.ptz_down_left
        "down" -> R.string.ptz_down; "down_right" -> R.string.ptz_down_right; "in" -> R.string.ptz_zoom_in; else -> R.string.ptz_zoom_out
    })
    val speed = listOf(.04, .08, .12, .2, .3)[level.roundToInt().coerceIn(1,5)-1]
    Box(Modifier.size(44.dp).background(if (selected) SoftAccent else ink.copy(alpha=.08f), RoundedCornerShape(10.dp))
        .testTag("ptz-$direction").semantics {
            role = Role.Button; contentDescription = label
            onClick { if (enabled && handle.press(action, direction, speed)) { handle.release(); true } else false }
        }.pointerInput(handle, direction, speed, enabled) {
            detectTapGestures(onPress = {
                if (enabled && handle.press(action, direction, speed)) {
                    var released = false
                    try { released = tryAwaitRelease(); if (released) handle.release() }
                    finally { if (!released) handle.stop() }
                }
            })
        }, contentAlignment = Alignment.Center) {
        if (direction in setOf("in", "out")) Text(if (direction == "in") "+" else "−", color = if (selected) Accent else ink, style = MaterialTheme.typography.titleLarge)
        else Canvas(Modifier.size(22.dp)) {
            val vector = when (direction) {
                "up_left" -> Offset(-1f,-1f); "up" -> Offset(0f,-1f); "up_right" -> Offset(1f,-1f)
                "left" -> Offset(-1f,0f); "right" -> Offset(1f,0f); "down_left" -> Offset(-1f,1f)
                "down" -> Offset(0f,1f); else -> Offset(1f,1f)
            }
            val color = if (selected) Accent else ink
            val center = Offset(size.width/2, size.height/2)
            val tip = center + vector * (size.width*.32f)
            val base = center - vector * (size.width*.32f)
            val backward = (base-tip) * .32f
            val side = Offset(-vector.y, vector.x) * (size.width*.2f)
            drawLine(color, base, tip, 1.8.dp.toPx())
            drawLine(color, tip, tip+backward+side, 1.8.dp.toPx())
            drawLine(color, tip, tip+backward-side, 1.8.dp.toPx())
        }
    }
}

enum class PlayerGlyph { PTZ, POINT, AREA, PRESETS, CALL, HANGUP, MIC, MIC_OFF }

@Composable fun PlayerGlyph(glyph: PlayerGlyph, color: Color, description: String? = null) {
    Canvas(Modifier.size(24.dp).semantics { if (description != null) contentDescription = description }) {
        val unit = size.width/24
        val stroke = Stroke(1.7f*unit)
        val path = Path()
        fun line(x0: Float, y0: Float, x1: Float, y1: Float) { path.moveTo(x0*unit, y0*unit); path.lineTo(x1*unit, y1*unit) }
        when (glyph) {
            PlayerGlyph.CALL -> {
                path.moveTo(5f*unit,3f*unit); path.lineTo(9f*unit,3f*unit); path.lineTo(11f*unit,8f*unit)
                path.lineTo(8f*unit,10f*unit); path.cubicTo(9f*unit,13f*unit,11f*unit,15f*unit,14f*unit,16f*unit)
                path.lineTo(16f*unit,13f*unit); path.lineTo(21f*unit,15f*unit); path.lineTo(21f*unit,19f*unit)
                path.cubicTo(21f*unit,25f*unit,0f*unit,13f*unit,3f*unit,5f*unit); path.close()
            }
            PlayerGlyph.HANGUP -> {
                path.moveTo(3f*unit,14f*unit); path.cubicTo(5f*unit,6f*unit,19f*unit,6f*unit,21f*unit,14f*unit)
                path.lineTo(17f*unit,17f*unit); path.lineTo(14f*unit,14f*unit); path.lineTo(14f*unit,11f*unit)
                path.lineTo(10f*unit,11f*unit); path.lineTo(10f*unit,14f*unit); path.lineTo(7f*unit,17f*unit); path.close()
            }
            PlayerGlyph.MIC, PlayerGlyph.MIC_OFF -> {
                path.moveTo(9f*unit,6f*unit); path.cubicTo(9f*unit,2f*unit,15f*unit,2f*unit,15f*unit,6f*unit)
                path.lineTo(15f*unit,11f*unit); path.cubicTo(15f*unit,15f*unit,9f*unit,15f*unit,9f*unit,11f*unit); path.close()
                path.moveTo(5f*unit,10f*unit); path.cubicTo(5f*unit,20f*unit,19f*unit,20f*unit,19f*unit,10f*unit)
                line(12f,18f,12f,22f); line(8f,22f,16f,22f)
                if (glyph == PlayerGlyph.MIC_OFF) line(3f,3f,21f,21f)
            }
            PlayerGlyph.PRESETS -> {
                path.moveTo(4f*unit,3f*unit); path.lineTo(17f*unit,3f*unit); path.lineTo(21f*unit,7f*unit)
                path.lineTo(21f*unit,21f*unit); path.lineTo(3f*unit,21f*unit); path.lineTo(3f*unit,4f*unit); path.close()
                line(7f,3f,7f,10f); line(7f,10f,16f,10f); line(16f,10f,16f,3f)
                line(7f,21f,7f,14f); line(7f,14f,17f,14f); line(17f,14f,17f,21f)
            }
            PlayerGlyph.PTZ -> {
                line(12f,3f,12f,21f); line(3f,12f,21f,12f)
                line(9f,6f,12f,3f); line(12f,3f,15f,6f); line(9f,18f,12f,21f); line(12f,21f,15f,18f)
                line(6f,9f,3f,12f); line(3f,12f,6f,15f); line(18f,9f,21f,12f); line(21f,12f,18f,15f)
            }
            PlayerGlyph.POINT -> {
                drawCircle(color, 6*unit, style = stroke)
                line(12f,2f,12f,8f); line(12f,16f,12f,22f); line(2f,12f,8f,12f); line(16f,12f,22f,12f)
            }
            PlayerGlyph.AREA -> {
                line(3f,8f,3f,3f); line(3f,3f,8f,3f); line(16f,3f,21f,3f); line(21f,3f,21f,8f)
                line(3f,16f,3f,21f); line(3f,21f,8f,21f); line(16f,21f,21f,21f); line(21f,21f,21f,16f)
                drawCircle(color, 4*unit, Offset(11*unit,11*unit), style=stroke); line(14f,14f,18f,18f)
            }
        }
        drawPath(path, color, style = stroke)
    }
}
