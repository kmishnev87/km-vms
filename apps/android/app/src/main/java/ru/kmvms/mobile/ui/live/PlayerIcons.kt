package ru.kmvms.mobile.ui.live

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import ru.kmvms.mobile.R

/** Same outlined speaker vocabulary as the web player, not an emoji. */
@Composable fun SpeakerIcon(enabled: Boolean, tint: Color) {
    val label = stringResource(if (enabled) R.string.sound_off else R.string.sound_on)
    Canvas(Modifier.size(24.dp).semantics { contentDescription = label }) {
        val unit = size.width / 24
        val body = Path().apply { moveTo(3*unit, 9*unit); lineTo(7*unit, 9*unit); lineTo(12*unit, 5*unit)
            lineTo(12*unit, 19*unit); lineTo(7*unit, 15*unit); lineTo(3*unit, 15*unit); close() }
        drawPath(body, tint, style = Stroke(1.7f*unit))
        if (enabled) {
            drawArc(tint, -60f, 120f, false, Offset(9*unit, 6*unit), androidx.compose.ui.geometry.Size(11*unit, 12*unit), style=Stroke(1.7f*unit))
        } else {
            drawLine(tint, Offset(16*unit, 9*unit), Offset(22*unit, 15*unit), 1.7f*unit)
            drawLine(tint, Offset(22*unit, 9*unit), Offset(16*unit, 15*unit), 1.7f*unit)
        }
    }
}
