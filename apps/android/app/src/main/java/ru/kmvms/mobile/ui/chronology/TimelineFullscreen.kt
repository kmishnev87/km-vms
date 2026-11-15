package ru.kmvms.mobile.ui.chronology

import android.app.Activity
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.media3.common.util.UnstableApi
import kotlinx.coroutines.delay
import ru.kmvms.mobile.R
import ru.kmvms.mobile.chronology.ChronologyController
import ru.kmvms.mobile.chronology.ChronologyState
import ru.kmvms.mobile.ui.live.SpeakerIcon
import java.time.Instant
import java.time.format.DateTimeFormatter

/** One unobtrusive archive surface for both event navigation and a timeline tile. */
@UnstableApi
@Composable internal fun TimelineFullscreen(controller: ChronologyController, state: ChronologyState, id: Long, close: () -> Unit) {
    val context=LocalContext.current
    var sound by remember(id){mutableStateOf(false)}
    var controls by remember(id){mutableStateOf(true)}
    var speeds by remember{mutableStateOf(false)}
    val track=controller.track(id)
    SideEffect{track?.player?.volume=if(sound)1f else 0f}
    DisposableEffect(context,track){
        val bars=(context as? Activity)?.window?.let{WindowCompat.getInsetsController(it,it.decorView)}
        bars?.systemBarsBehavior=WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        bars?.hide(WindowInsetsCompat.Type.systemBars())
        onDispose{bars?.show(WindowInsetsCompat.Type.systemBars());track?.player?.volume=0f}
    }
    LaunchedEffect(controls,state.running,speeds){if(controls && state.running && !speeds){delay(4000);controls=false}}
    val overlay=Color(0xAA233044)
    Box(Modifier.fillMaxSize().background(Color.Black)) {
        TimelineVideo(controller,state,id,Modifier.fillMaxSize()){controls=!controls}
        if(controls || !state.wantsPlay) {
            Row(Modifier.align(Alignment.TopCenter).fillMaxWidth().padding(10.dp),horizontalArrangement=Arrangement.SpaceBetween){
                IconButton(onClick=close,modifier=Modifier.background(overlay,RoundedCornerShape(12.dp))){Icon(Icons.AutoMirrored.Filled.ArrowBack,stringResource(R.string.back),tint=Color.White)}
                IconButton(onClick={sound=!sound},modifier=Modifier.background(overlay,RoundedCornerShape(12.dp))){SpeakerIcon(sound,Color.White)}
            }
            Row(Modifier.align(Alignment.BottomCenter).fillMaxWidth().background(overlay).navigationBarsPadding().padding(horizontal=6.dp),
                verticalAlignment=Alignment.CenterVertically) {
                IconButton(onClick=controller::toggle,modifier=Modifier.testTag("timeline-fullscreen-play")){
                    if(state.wantsPlay)Text("Ⅱ",color=Color.White,style=MaterialTheme.typography.titleLarge)
                    else Icon(Icons.Default.PlayArrow,stringResource(R.string.play),tint=Color.White)
                }
                TextButton(onClick={controller.seek(state.position-10_000)},contentPadding=PaddingValues(4.dp),modifier=Modifier.width(44.dp).testTag("timeline-back-ten")){Text("−10",color=Color.White)}
                Column(Modifier.weight(1f),horizontalAlignment=Alignment.CenterHorizontally){
                    Text(Instant.ofEpochMilli(state.position).atZone(state.zone).format(DateTimeFormatter.ofPattern("HH:mm:ss")),color=Color.White,style=MaterialTheme.typography.labelMedium)
                    Text(Instant.ofEpochMilli(state.position).atZone(state.zone).format(DateTimeFormatter.ofPattern("dd.MM.yyyy")),color=Color.White.copy(alpha=.7f),style=MaterialTheme.typography.labelSmall)
                }
                TextButton(onClick={controller.seek(state.position+10_000)},contentPadding=PaddingValues(4.dp),modifier=Modifier.width(44.dp).testTag("timeline-forward-ten")){Text("+10",color=Color.White)}
                Box{
                    TextButton(onClick={speeds=true},contentPadding=PaddingValues(4.dp),modifier=Modifier.width(48.dp).testTag("timeline-fullscreen-speed")){Text("${state.speed.toString().removeSuffix(".0")}×",color=Color.White)}
                    DropdownMenu(speeds,{speeds=false}){listOf(.25f,.5f,1f,2f,4f).forEach{speed->
                        DropdownMenuItem(text={Text("${speed.toString().removeSuffix(".0")}×")},onClick={controller.speed(speed);speeds=false})
                    }}
                }
            }
        }
    }
}
