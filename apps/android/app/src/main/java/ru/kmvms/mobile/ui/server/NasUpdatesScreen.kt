package ru.kmvms.mobile.ui.server

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.server.*
import ru.kmvms.mobile.ui.Muted

@Composable fun NasUpdatesScreen(api: NasApi, namespace: String, close: ()->Unit, message:(String)->Unit) {
    val context=LocalContext.current
    val controller=remember(api,namespace){NasUpdatesController(context,NasUpdatesApi(api),namespace)}
    val state by controller.state.collectAsStateWithLifecycle()
    var confirm by remember{mutableStateOf<NasUpdateCandidate?>(null)}
    val lifecycle=LocalLifecycleOwner.current.lifecycle
    val error=stringResource(R.string.server_error)
    BackHandler{close()}
    DisposableEffect(controller,lifecycle){
        controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED))
        val observer=LifecycleEventObserver{_,_->controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED))}
        lifecycle.addObserver(observer)
        onDispose{lifecycle.removeObserver(observer);controller.close()}
    }
    LaunchedEffect(state.failures){if(state.failures>0)message(error)}
    Column(Modifier.fillMaxSize()) {
        Row(verticalAlignment=Alignment.CenterVertically){
            IconButton(onClick=close){Icon(Icons.AutoMirrored.Filled.ArrowBack,stringResource(R.string.back))}
            Text(stringResource(R.string.nas_updates),style=MaterialTheme.typography.titleLarge)
        }
        Box(Modifier.height(6.dp).fillMaxWidth()){if(state.loading||state.posting)LinearProgressIndicator(Modifier.fillMaxWidth())}
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(16.dp),verticalArrangement=Arrangement.spacedBy(14.dp)){
            Panel {
                Text(stringResource(R.string.nas_installed_version,state.check?.installed?:"—"),style=MaterialTheme.typography.titleMedium)
                Text(stringResource(when{state.checking->R.string.nas_checking;state.check?.candidate!=null->R.string.nas_update_available;state.check?.status=="current"->R.string.nas_current;else->R.string.nas_update_check_needed}),color=Muted)
                state.check?.candidate?.let{Text(it.version,style=MaterialTheme.typography.titleLarge)}
                Row(horizontalArrangement=Arrangement.spacedBy(8.dp)){
                    OutlinedButton(onClick={controller.refresh(true)},enabled=!state.loading&&!state.posting&&state.pending==null,modifier=Modifier.weight(1f).testTag("nas-update-check")){Text(stringResource(R.string.mobile_update_check))}
                    Button(onClick={confirm=state.check?.candidate},enabled=state.check?.candidate!=null&&state.pending==null&&!state.posting&&!state.loading,modifier=Modifier.weight(1f).testTag("nas-update-apply")){Text(stringResource(R.string.nas_update))}
                }
            }
            val progress=state.progress
            if(progress?.visible==true||state.pending!=null)Panel{
                val success=progress?.completed(state.pending)==true
                val failed=progress?.terminal==true&&!success
                Text(stringResource(when{success->R.string.operation_completed;failed->R.string.operation_failed;state.reconnecting->R.string.nas_reconnecting;progress?.visible==true||state.posting->R.string.operation_running;else->R.string.operation_unknown}),
                    color=if(success)Color(0xFF15803D)else if(failed)MaterialTheme.colorScheme.error else Muted,modifier=Modifier.testTag("nas-update-result"))
                val percent=if(success)100 else progress?.percent
                if(percent!=null)LinearProgressIndicator(progress={percent/100f},color=if(success)Color(0xFF15803D)else MaterialTheme.colorScheme.primary,modifier=Modifier.fillMaxWidth())
                else if(state.watching||state.posting)LinearProgressIndicator(Modifier.fillMaxWidth())
                progress?.elapsed?.let{Text(stringResource(R.string.nas_elapsed,it/60,it%60),color=Muted,style=MaterialTheme.typography.bodySmall)}
                if(state.reconnecting||progress?.status=="completed"&&!success)Text(stringResource(R.string.operation_unknown),color=Muted,style=MaterialTheme.typography.bodySmall)
                Row(horizontalArrangement=Arrangement.spacedBy(8.dp)){
                    OutlinedButton(onClick={controller.refresh()},enabled=!state.loading&&!state.posting,modifier=Modifier.weight(1f)){Text(stringResource(R.string.check_status))}
                    if(state.pending!=null&&!state.watching)OutlinedButton(onClick=controller::retry,enabled=!state.posting,modifier=Modifier.weight(1f)){Text(stringResource(R.string.retry))}
                    if(progress?.canCancel==true)OutlinedButton(onClick=controller::cancel,enabled=!state.posting,modifier=Modifier.weight(1f)){Text(stringResource(R.string.cancel))}
                }
            }
        }
    }
    confirm?.let{candidate->AlertDialog(onDismissRequest={confirm=null},title={Text(stringResource(R.string.nas_updates))},
        text={Text(stringResource(R.string.nas_update_confirmation,candidate.version))},
        confirmButton={TextButton(onClick={confirm=null;controller.start(candidate)},modifier=Modifier.testTag("confirm-nas-update")){Text(stringResource(R.string.nas_update))}},
        dismissButton={TextButton(onClick={confirm=null}){Text(stringResource(R.string.cancel))}})}
}
