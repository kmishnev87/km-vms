package ru.kmvms.mobile.ui.archive

import android.app.Activity
import androidx.activity.compose.BackHandler
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
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.unit.dp
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.media3.common.util.UnstableApi
import ru.kmvms.mobile.R
import ru.kmvms.mobile.archive.ArchivePlaybackSession
import ru.kmvms.mobile.archive.Recording
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.ui.live.SpeakerIcon
import ru.kmvms.mobile.ui.live.VideoSurface

@UnstableApi
@Composable fun RecordingPlayer(api: NasApi, recording: Recording, initialPosition: Long, savePosition: (Long) -> Unit,
                                close: () -> Unit, immersive: (Boolean) -> Unit) {
    val context=LocalContext.current
    val playback=remember(api, recording.identityKey) { ArchivePlaybackSession(context,api,recording,initialPosition) }
    val video by playback.video.collectAsStateWithLifecycle()
    val position by playback.position.collectAsStateWithLifecycle()
    val playLabel=stringResource(if(position.playing)R.string.pause else R.string.play)
    val lifecycle=LocalLifecycleOwner.current.lifecycle
    var sound by remember { mutableStateOf(false) }
    var scrub by remember { mutableStateOf<Float?>(null) }
    SideEffect { playback.player.volume=if(sound)1f else 0f }
    LaunchedEffect(Unit) { immersive(true) }
    BackHandler { close() }
    DisposableEffect(playback,lifecycle) {
        val observer=LifecycleEventObserver { _,_ -> playback.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer); savePosition(playback.close()); immersive(false) }
    }
    DisposableEffect(context) {
        val bars=(context as? Activity)?.window?.let { WindowCompat.getInsetsController(it,it.decorView) }
        bars?.systemBarsBehavior=WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        bars?.hide(WindowInsetsCompat.Type.systemBars())
        onDispose { bars?.show(WindowInsetsCompat.Type.systemBars()) }
    }
    Box(Modifier.fillMaxSize().background(Color.Black)) {
        VideoSurface(playback.player,video,playback::retry,Modifier.fillMaxSize(),zoomable=true)
        IconButton(onClick=close,modifier=Modifier.align(Alignment.TopStart).padding(10.dp).background(Color(0xAA233044),RoundedCornerShape(12.dp))) {
            Icon(Icons.AutoMirrored.Filled.ArrowBack,stringResource(R.string.back),tint=Color.White)
        }
        IconButton(onClick={sound=!sound},modifier=Modifier.align(Alignment.TopEnd).padding(10.dp).background(Color(0xAA233044),RoundedCornerShape(12.dp))) {
            SpeakerIcon(sound,Color.White)
        }
        Row(Modifier.align(Alignment.BottomCenter).fillMaxWidth().background(Color(0xBA233044)).navigationBarsPadding().padding(8.dp),
            verticalAlignment=Alignment.CenterVertically) {
            IconButton(onClick=playback::toggle,modifier=Modifier.testTag("archive-play-pause").semantics{contentDescription=playLabel}) {
                if(position.playing) Text("Ⅱ",color=Color.White,style=MaterialTheme.typography.titleLarge)
                else Icon(Icons.Default.PlayArrow,stringResource(R.string.play),tint=Color.White)
            }
            Text(clock(position.position),color=Color.White,style=MaterialTheme.typography.labelMedium,modifier=Modifier.testTag("archive-position"))
            Slider(value=scrub?:position.position.toFloat().coerceAtMost(position.duration.toFloat()),onValueChange={scrub=it},
                onValueChangeFinished={scrub?.let{playback.seek(it.toLong())};scrub=null},
                valueRange=0f..position.duration.toFloat().coerceAtLeast(1f),enabled=position.duration>0,
                modifier=Modifier.weight(1f).padding(horizontal=8.dp).testTag("archive-seek"))
            Text(clock(position.duration),color=Color.White,style=MaterialTheme.typography.labelMedium)
        }
    }
}

fun clock(milliseconds:Long):String {
    val seconds=milliseconds.coerceAtLeast(0)/1000
    return if(seconds>=3600) "%d:%02d:%02d".format(seconds/3600,seconds/60%60,seconds%60) else "%d:%02d".format(seconds/60,seconds%60)
}
