package ru.kmvms.mobile.ui.events

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import ru.kmvms.mobile.R
import ru.kmvms.mobile.events.CameraEventKind
import ru.kmvms.mobile.events.AiEventCondition
import ru.kmvms.mobile.ui.Accent
import androidx.compose.material3.Icon
import androidx.compose.ui.res.painterResource

internal fun CameraEventKind.label(): Int = when (this) {
    CameraEventKind.MOTION -> R.string.event_motion
    CameraEventKind.PERSON -> R.string.event_person
    CameraEventKind.VEHICLE -> R.string.event_vehicle
    CameraEventKind.ANIMAL -> R.string.event_animal
    CameraEventKind.INTRUSION -> R.string.event_intrusion
    CameraEventKind.LINE_CROSSING -> R.string.event_line_crossing
    CameraEventKind.LOITERING -> R.string.event_loitering
    CameraEventKind.TAMPER -> R.string.event_tamper
    CameraEventKind.AUDIO -> R.string.event_audio
    CameraEventKind.WRONG_DIRECTION -> R.string.event_wrong_direction
    CameraEventKind.FIRE -> R.string.event_fire
    CameraEventKind.OTHER -> R.string.event_other
}

internal fun AiEventCondition.label(): Int = when (this) {
    AiEventCondition.PRESENCE -> R.string.ai_event_presence
    AiEventCondition.ENTER -> R.string.ai_event_enter
    AiEventCondition.EXIT -> R.string.ai_event_exit
    AiEventCondition.CROSSING -> R.string.event_line_crossing
    AiEventCondition.DWELL -> R.string.event_loitering
    AiEventCondition.SCENE -> R.string.ai_event_scene
}

internal fun eventLabels(kind: CameraEventKind, objects: Set<CameraEventKind>, condition: AiEventCondition?): List<Int> =
    (listOf(condition?.takeUnless { it == AiEventCondition.PRESENCE }?.label() ?: kind.label())
        + objects.map { it.label() }).distinct()

/** Same camera/clock vocabulary as the existing web event actions. */
@Composable internal fun EventActionIcon(live: Boolean, color: Color = Accent) {
    Icon(painterResource(if(live)R.drawable.ic_event_live else R.drawable.ic_event_archive),null,Modifier.size(22.dp),tint=color)
}
