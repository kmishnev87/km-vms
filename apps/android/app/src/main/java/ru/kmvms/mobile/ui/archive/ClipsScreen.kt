package ru.kmvms.mobile.ui.archive

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.text.format.Formatter
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import ru.kmvms.mobile.R
import ru.kmvms.mobile.archive.*
import ru.kmvms.mobile.ui.Muted
import java.time.ZoneId
import java.time.format.DateTimeFormatter

@OptIn(ExperimentalMaterial3Api::class)
@Composable fun ClipsScreen(api: ArchiveApi, cameras: List<ArchiveCamera>, zone: ZoneId,
                            close: () -> Unit, message: (String) -> Unit) {
    val controller=remember(api){ClipsController(api)}
    val state by controller.state.collectAsStateWithLifecycle()
    val context=LocalContext.current
    val lifecycle=LocalLifecycleOwner.current.lifecycle
    val scope=rememberCoroutineScope()
    var tab by rememberSaveable{mutableIntStateOf(0)}
    var confirmation by remember{mutableStateOf<Clip?>(null)}
    var pending by remember{mutableStateOf<Clip?>(null)}
    var downloading by remember{mutableStateOf(false)}
    val failure=stringResource(R.string.server_error)
    val started=stringResource(R.string.download_started)
    fun download(clip:Clip){ if(downloading)return;downloading=true;scope.launch{
        try{downloadClip(context,api,clip);message(started)}catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){message(failure)}finally{downloading=false}
    } }
    val storagePermission=rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()){granted->
        pending?.let{if(granted)download(it)else message(failure)};pending=null
    }
    LaunchedEffect(state.failures){if(state.failures>0)message(failure)}
    DisposableEffect(controller,lifecycle){
        controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED))
        val observer=LifecycleEventObserver{_,_->controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED))}
        lifecycle.addObserver(observer)
        onDispose{lifecycle.removeObserver(observer);controller.close()}
    }
    Column(Modifier.fillMaxSize()) {
        Row(Modifier.fillMaxWidth(),verticalAlignment=Alignment.CenterVertically){
            IconButton(onClick=close){Icon(Icons.AutoMirrored.Filled.ArrowBack,stringResource(R.string.back))}
            Text(stringResource(R.string.clips),style=MaterialTheme.typography.titleLarge,modifier=Modifier.weight(1f))
            IconButton(onClick={controller.refresh()},enabled=!state.loading){Icon(Icons.Default.Refresh,stringResource(R.string.refresh))}
        }
        SecondaryTabRow(selectedTabIndex=tab){
            Tab(selected=tab==0,onClick={tab=0},text={Text(stringResource(R.string.saved_clips))})
            Tab(selected=tab==1,onClick={tab=1},modifier=Modifier.testTag("clip-create-tab"),text={Text(stringResource(R.string.create_clip))})
        }
        Box(Modifier.fillMaxWidth().height(6.dp)){if(state.loading||state.working||downloading)LinearProgressIndicator(Modifier.fillMaxWidth())}
        if(tab==1)ClipForm(cameras,zone,state.maxDuration,state.working){camera,title,start,end->controller.create(camera,title,start,end);tab=0}
        else LazyColumn(Modifier.fillMaxSize(),contentPadding=PaddingValues(16.dp),verticalArrangement=Arrangement.spacedBy(12.dp)){
            if(state.items.isEmpty()&&!state.loading)item{Text(stringResource(R.string.no_saved_clips),color=Muted)}
            items(state.items,key={it.id}){clip->Surface(shape=RoundedCornerShape(14.dp),color=Color.White,modifier=Modifier.fillMaxWidth()){
                Column(Modifier.padding(14.dp),verticalArrangement=Arrangement.spacedBy(8.dp)){
                    Text(clip.title.ifBlank{stringResource(R.string.clip)},fontWeight=FontWeight.SemiBold)
                    Text(clip.camera,color=Muted,style=MaterialTheme.typography.bodyMedium)
                    clip.start?.let { start -> parseTime(start,zone)?.let { instant ->
                        val beginning=instant.atZone(zone)
                        val ending=clip.end?.let{parseTime(it,zone)}?.atZone(zone)
                        Text(beginning.format(DateTimeFormatter.ofPattern("dd.MM.yyyy HH:mm")) +
                            (ending?.let{" — "+it.format(DateTimeFormatter.ofPattern(if(it.toLocalDate()==beginning.toLocalDate())"HH:mm" else "dd.MM.yyyy HH:mm"))}?:""),
                            style=MaterialTheme.typography.labelMedium,color=Muted)
                    } }
                    ClipFacts(clip)
                    if(clip.gaps>0||clip.errorCode=="source_gap_detected")Text(stringResource(R.string.clip_has_gaps),style=MaterialTheme.typography.labelMedium,color=MaterialTheme.colorScheme.error)
                    if(clip.status=="running")LinearProgressIndicator(progress={clip.progress.coerceIn(0,100)/100f},modifier=Modifier.fillMaxWidth())
                    Row(Modifier.fillMaxWidth(),horizontalArrangement=Arrangement.spacedBy(8.dp)){
                        OutlinedButton(onClick={
                            if(Build.VERSION.SDK_INT<=28&&ContextCompat.checkSelfPermission(context,Manifest.permission.WRITE_EXTERNAL_STORAGE)!=PackageManager.PERMISSION_GRANTED){pending=clip;storagePermission.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE)}
                            else download(clip)
                        },enabled=clip.status=="done"&&!state.working&&!downloading,modifier=Modifier.weight(1f).testTag("download-clip-${clip.id}")){Text(stringResource(R.string.download))}
                        OutlinedButton(onClick={confirmation=clip},enabled=!state.working&&clip.status!="running",modifier=Modifier.weight(1f).testTag("delete-clip-${clip.id}")){Text(stringResource(R.string.remove))}
                    }
                    if(clip.status=="queued")TextButton(onClick={controller.generate(clip)},enabled=!state.working){Text(stringResource(R.string.create_clip))}
                }
            }}
            if(state.more)item{OutlinedButton(onClick={controller.refresh(true)},enabled=!state.loading,modifier=Modifier.fillMaxWidth()){Text(stringResource(R.string.load_more))}}
        }
    }
    confirmation?.let{clip->AlertDialog(onDismissRequest={confirmation=null},modifier=Modifier.semantics{testTagsAsResourceId=true},text={Text(stringResource(R.string.delete_clip_confirmation))},
        confirmButton={TextButton(onClick={confirmation=null;controller.delete(clip)},modifier=Modifier.testTag("confirm-delete-clip")){Text(stringResource(R.string.remove))}},
        dismissButton={TextButton(onClick={confirmation=null}){Text(stringResource(R.string.cancel))}})}
}

@Composable internal fun ClipFacts(clip: Clip) {
    val context=LocalContext.current
    if(clip.status=="done") Row(Modifier.fillMaxWidth().testTag("clip-facts-${clip.id}"),horizontalArrangement=Arrangement.spacedBy(12.dp)) {
        Text(stringResource(R.string.clip_size,if(clip.size>0)Formatter.formatShortFileSize(context,clip.size)else"—"),color=Muted,
            style=MaterialTheme.typography.labelMedium,modifier=Modifier.weight(1f).testTag("clip-size-${clip.id}"))
        Text(stringResource(R.string.clip_duration,clipDuration(clip.durationSeconds)),color=Muted,
            style=MaterialTheme.typography.labelMedium,modifier=Modifier.weight(1f).testTag("clip-duration-${clip.id}"))
    } else Text(stringResource(when(clip.status){"queued"->R.string.clip_queued;"running"->R.string.clip_running;else->R.string.clip_failed}),color=Muted,style=MaterialTheme.typography.labelMedium)
}
