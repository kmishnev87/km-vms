package ru.kmvms.mobile.ui.archive

import android.app.DatePickerDialog
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import ru.kmvms.mobile.R
import ru.kmvms.mobile.archive.ArchiveCamera
import ru.kmvms.mobile.archive.zoneLabel
import java.time.*
import java.time.format.DateTimeFormatter

@Composable fun ClipForm(cameras: List<ArchiveCamera>, zone: ZoneId, maxDuration: Long?, busy: Boolean,
                         create: (Long,String,OffsetDateTime,OffsetDateTime) -> Unit) {
    var cameraId by rememberSaveable { mutableStateOf<Long?>(null) }
    var menu by remember { mutableStateOf(false) }
    var title by rememberSaveable { mutableStateOf("") }
    var startText by rememberSaveable { mutableStateOf(LocalDateTime.now(zone).minusMinutes(5).withSecond(0).withNano(0).toString()) }
    var endText by rememberSaveable { mutableStateOf(LocalDateTime.now(zone).withSecond(0).withNano(0).toString()) }
    val start=LocalDateTime.parse(startText).atZone(zone).toOffsetDateTime()
    val end=LocalDateTime.parse(endText).atZone(zone).toOffsetDateTime()
    val duration=Duration.between(start,end).seconds
    val valid=cameraId!=null && duration>0 && maxDuration!=null && duration<=maxDuration
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),verticalArrangement=Arrangement.spacedBy(14.dp)) {
        Box(Modifier.fillMaxWidth()) {
            OutlinedButton(onClick={menu=true},modifier=Modifier.fillMaxWidth().height(48.dp).testTag("clip-camera")) {
                Text(cameras.find{it.id==cameraId}?.name?:stringResource(R.string.choose_camera))
            }
            DropdownMenu(menu,{menu=false}) { cameras.forEach{camera -> DropdownMenuItem(text={Text(camera.name)},onClick={cameraId=camera.id;menu=false})} }
        }
        OutlinedTextField(title,{title=it},label={Text(stringResource(R.string.clip_title))},singleLine=true,modifier=Modifier.fillMaxWidth().testTag("clip-title"))
        ClipMoment(R.string.from_time,LocalDateTime.parse(startText)){startText=it.toString()}
        ClipMoment(R.string.to_time,LocalDateTime.parse(endText)){endText=it.toString()}
        if(maxDuration!=null)Text(stringResource(R.string.clip_limit,maxDuration/3600,zoneLabel(zone)),style=MaterialTheme.typography.labelMedium)
        Button(onClick={create(cameraId!!,title,start,end)},enabled=valid&&!busy,modifier=Modifier.fillMaxWidth().height(48.dp).testTag("create-clip")) {
            Text(stringResource(R.string.create_clip))
        }
    }
}

@Composable private fun ClipMoment(label: Int, value: LocalDateTime, update: (LocalDateTime) -> Unit) {
    val context=LocalContext.current
    val prefix=if(label==R.string.from_time)"clip-start" else "clip-end"
    var editTime by remember { mutableStateOf(false) }
    Row(Modifier.fillMaxWidth(),horizontalArrangement=Arrangement.spacedBy(8.dp)) {
        OutlinedButton(onClick={DatePickerDialog(context,{_,year,month,day->update(LocalDate.of(year,month+1,day).atTime(value.toLocalTime()))},value.year,value.monthValue-1,value.dayOfMonth).show()},modifier=Modifier.weight(1.5f).testTag("$prefix-date")) {
            Text(stringResource(label)+"  "+value.toLocalDate().format(DateTimeFormatter.ofPattern("dd.MM.yyyy")))
        }
        OutlinedButton(onClick={editTime=true},modifier=Modifier.weight(1f).testTag("$prefix-time")) {
            Text(value.toLocalTime().format(DateTimeFormatter.ofPattern("HH:mm:ss")))
        }
    }
    if(editTime) ClipTimeDialog(prefix,value.toLocalTime(),{editTime=false}) { time ->
        update(value.toLocalDate().atTime(time));editTime=false
    }
}

/** Clips need second precision: a minute-only picker cannot select a short recording. */
@Composable internal fun ClipTimeDialog(prefix: String, initial: LocalTime, dismiss: () -> Unit, choose: (LocalTime) -> Unit) {
    var hour by remember { mutableStateOf("%02d".format(initial.hour)) }
    var minute by remember { mutableStateOf("%02d".format(initial.minute)) }
    var second by remember { mutableStateOf("%02d".format(initial.second)) }
    val parsed=try { LocalTime.of(hour.toInt(),minute.toInt(),second.toInt()) } catch(_:Exception) { null }
    AlertDialog(onDismissRequest=dismiss,modifier=Modifier.semantics{testTagsAsResourceId=true},title={Text(stringResource(R.string.choose_time))},text={
        Row(Modifier.fillMaxWidth(),horizontalArrangement=Arrangement.spacedBy(8.dp)) {
            listOf(Triple(R.string.hours,hour,{v:String->hour=v}),Triple(R.string.minutes,minute,{v:String->minute=v}),Triple(R.string.seconds,second,{v:String->second=v})).forEachIndexed { index,field ->
                OutlinedTextField(field.second,{new->if(new.length<=2&&new.all{it.isDigit()})field.third(new)},
                    label={Text(stringResource(field.first))},singleLine=true,
                    keyboardOptions=KeyboardOptions(keyboardType=KeyboardType.Number),
                    modifier=Modifier.weight(1f).testTag("$prefix-"+listOf("hour","minute","second")[index]))
            }
        }
    },confirmButton={TextButton(onClick={parsed?.let(choose)},enabled=parsed!=null,modifier=Modifier.testTag("$prefix-time-confirm")){Text(stringResource(R.string.apply))}},
        dismissButton={TextButton(onClick=dismiss){Text(stringResource(R.string.cancel))}})
}
