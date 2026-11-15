package ru.kmvms.mobile.ui.chronology

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.drawIntoCanvas
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import ru.kmvms.mobile.R
import ru.kmvms.mobile.chronology.ChronologyState
import ru.kmvms.mobile.core.TimelineViewport
import ru.kmvms.mobile.data.Camera
import ru.kmvms.mobile.ui.Accent
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.Outline
import java.time.Instant
import java.time.format.DateTimeFormatter

@Composable internal fun TimelineRuler(state: ChronologyState, cameras: List<Camera>, seek: (Long) -> Unit) {
    val dayStart=state.day.atStartOfDay(state.zone).toInstant().toEpochMilli()
    val dayEnd=state.day.plusDays(1).atStartOfDay(state.zone).toInstant().toEpochMilli()
    var scale by rememberSaveable { mutableIntStateOf(3) }
    var origin by rememberSaveable(dayStart, scale) {
        mutableLongStateOf(TimelineViewport.around(dayStart,dayEnd,state.position,TimelineViewport.scales[scale]*60_000L).start)
    }
    val viewport=TimelineViewport.around(dayStart,dayEnd,origin+TimelineViewport.scales[scale]*30_000L,TimelineViewport.scales[scale]*60_000L)
    var preview by remember { mutableStateOf<Long?>(null) }
    var dragOrigin by remember { mutableLongStateOf(origin) }
    var dragPosition by remember { mutableLongStateOf(state.position) }
    var totalDrag by remember { mutableFloatStateOf(0f) }
    val current=preview ?: state.position
    val latestSeek by rememberUpdatedState(seek)
    val latestPosition by rememberUpdatedState(state.position)
    val density=LocalDensity.current
    val paint=remember(density){android.graphics.Paint(android.graphics.Paint.ANTI_ALIAS_FLAG).apply {
        color=android.graphics.Color.rgb(100,116,139);textSize=with(density){10.dp.toPx()}
    }}
    LaunchedEffect(state.position,dayStart,scale) {
        if(preview==null && (state.position<viewport.start || state.position>=viewport.end))
            origin=viewport.centred(state.position).start
    }
    Column {
        Row(Modifier.fillMaxWidth().height(36.dp),verticalAlignment=Alignment.CenterVertically) {
            Text(Instant.ofEpochMilli(current).atZone(state.zone).format(DateTimeFormatter.ofPattern("HH:mm:ss")),
                color=Muted,style=MaterialTheme.typography.labelMedium,modifier=Modifier.weight(1f).testTag("timeline-ruler-time"))
            TextButton(onClick={scale=(scale-1).coerceAtLeast(0)},enabled=scale>0,contentPadding=PaddingValues(0.dp),modifier=Modifier.size(40.dp,36.dp).testTag("timeline-zoom-out")){Text("−")}
            val minutes=TimelineViewport.scales[scale]
            Text(stringResource(if(minutes>=60)R.string.timeline_hours else R.string.timeline_minutes,if(minutes>=60)minutes/60 else minutes),
                color=Muted,style=MaterialTheme.typography.labelMedium,modifier=Modifier.widthIn(min=44.dp))
            TextButton(onClick={scale=(scale+1).coerceAtMost(TimelineViewport.scales.lastIndex)},enabled=scale<TimelineViewport.scales.lastIndex,
                contentPadding=PaddingValues(0.dp),modifier=Modifier.size(40.dp,36.dp).testTag("timeline-zoom-in")){Text("+")}
        }
        Row(Modifier.fillMaxWidth()) {
            Column(Modifier.width(82.dp).padding(top=24.dp)) {
                state.selected.forEach { id ->
                    Box(Modifier.height(20.dp),contentAlignment=Alignment.CenterStart) {
                        Text(cameras.find{it.id==id}?.name.orEmpty(),maxLines=1,overflow=TextOverflow.Ellipsis,
                            color=Muted,style=MaterialTheme.typography.labelSmall,modifier=Modifier.padding(end=6.dp))
                    }
                }
            }
            Canvas(Modifier.weight(1f).height((24+state.selected.size.coerceAtLeast(1)*20).dp).testTag("timeline-ranges")
                .pointerInput(viewport,state.selected,state.ranges) {
                    detectTapGestures { point ->
                        val row=((point.y-24.dp.toPx())/20.dp.toPx()).toInt().coerceIn(0,(state.selected.size-1).coerceAtLeast(0))
                        val ranges=state.ranges[state.selected.getOrNull(row)].orEmpty().map{it.start to it.end}
                        latestSeek(viewport.hit(point.x/size.width,size.width.toFloat(),ranges))
                    }
                }
                .pointerInput(viewport.dayStart,viewport.span) {
                    detectDragGestures(onDragStart={dragOrigin=origin;dragPosition=latestPosition;totalDrag=0f;preview=latestPosition},
                        onDragCancel={origin=dragOrigin;preview=null},onDragEnd={preview?.let(latestSeek);preview=null}) { change, amount ->
                        change.consume();totalDrag+=amount.x
                        val delta=(-totalDrag/size.width*viewport.span).toLong()
                        val initial=TimelineViewport(dayStart,dayEnd,dragOrigin,viewport.span)
                        origin=initial.shifted(delta).start
                        preview=(dragPosition+origin-dragOrigin).coerceIn(dayStart,dayEnd-1)
                    }
                }) {
                val ticks=viewport.ticks((size.width/56.dp.toPx()).toInt().coerceAtLeast(2))
                ticks.forEach { time ->
                    val x=viewport.fraction(time)*size.width
                    drawLine(Outline,Offset(x,19.dp.toPx()),Offset(x,size.height),1.dp.toPx())
                    val label=Instant.ofEpochMilli(time).atZone(state.zone).format(DateTimeFormatter.ofPattern("HH:mm"))
                    drawIntoCanvas { canvas -> canvas.nativeCanvas.drawText(label,(x-paint.measureText(label)/2).coerceIn(0f,(size.width-paint.measureText(label)).coerceAtLeast(0f)),13.dp.toPx(),paint) }
                }
                state.selected.forEachIndexed { index,id ->
                    val y=24.dp.toPx()+index*20.dp.toPx()+3.dp.toPx()
                    drawRect(Outline.copy(alpha=.6f),Offset(0f,y),Size(size.width,14.dp.toPx()))
                    state.ranges[id].orEmpty().forEach { range ->
                        if(range.end>viewport.start && range.start<viewport.end) {
                            val left=viewport.fraction(range.start).coerceIn(0f,1f)*size.width
                            val right=viewport.fraction(range.end).coerceIn(0f,1f)*size.width
                            drawRect(Accent.copy(alpha=.65f),Offset(left,y),Size((right-left).coerceAtLeast(2f),14.dp.toPx()))
                        }
                    }
                }
                if(current in viewport.start..viewport.end) {
                    val x=viewport.fraction(current)*size.width
                    drawLine(Accent,Offset(x,18.dp.toPx()),Offset(x,size.height),2.dp.toPx())
                }
            }
        }
    }
}
