package ru.kmvms.mobile.ui.server

import android.text.format.Formatter
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import ru.kmvms.mobile.R
import ru.kmvms.mobile.archive.parseTime
import ru.kmvms.mobile.server.*
import ru.kmvms.mobile.ui.Muted
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

@Composable fun BackupsScreen(api:ServerApi,namespace:String,zoneName:String,close:()->Unit,message:(String)->Unit) {
    val context=LocalContext.current
    val controller=remember(api,namespace){BackupsController(context,api,namespace)}
    val state by controller.state.collectAsStateWithLifecycle()
    var confirm by remember{mutableStateOf<Pair<String,String?>?>(null)}
    val lifecycle=LocalLifecycleOwner.current.lifecycle
    val failure=stringResource(R.string.server_error)
    val zone=remember(zoneName){runCatching{ZoneId.of(zoneName)}.getOrDefault(ZoneOffset.UTC)}
    BackHandler{close()}
    DisposableEffect(controller,lifecycle){
        controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED))
        val observer=LifecycleEventObserver{_,_->controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED))}
        lifecycle.addObserver(observer)
        onDispose{lifecycle.removeObserver(observer);controller.close()}
    }
    LaunchedEffect(state.failures){if(state.failures>0)message(failure)}
    Column(Modifier.fillMaxSize()){
        Row(verticalAlignment=Alignment.CenterVertically){
            IconButton(onClick=close){Icon(Icons.AutoMirrored.Filled.ArrowBack,stringResource(R.string.back))}
            Text(stringResource(R.string.backups),style=MaterialTheme.typography.titleLarge,modifier=Modifier.weight(1f))
            TextButton(onClick={confirm="create" to null},enabled=state.pending==null,modifier=Modifier.testTag("backup-create")){Text(stringResource(R.string.create_backup))}
        }
        Box(Modifier.fillMaxWidth().height(6.dp)){if(state.loading||state.polling)LinearProgressIndicator(Modifier.fillMaxWidth())}
        LazyColumn(Modifier.fillMaxSize().testTag("backup-content"),contentPadding=PaddingValues(16.dp),verticalArrangement=Arrangement.spacedBy(12.dp)){
            item{Text(stringResource(R.string.backup_scope),style=MaterialTheme.typography.bodySmall,color=Muted)}
            if(state.operationState!=null)item{Panel{
                Text(stringResource(when(state.operationState){"completed"->R.string.operation_completed;"running"->R.string.operation_running;"unknown"->R.string.operation_unknown;else->R.string.operation_failed}),color=if(state.operationState in setOf("failed","blocked","interrupted"))MaterialTheme.colorScheme.error else Muted)
                if(state.pending!=null&&!state.polling)Row(horizontalArrangement=Arrangement.spacedBy(8.dp)){
                    OutlinedButton(onClick=controller::check,modifier=Modifier.weight(1f)){Text(stringResource(R.string.check_status))}
                    OutlinedButton(onClick=controller::retry,modifier=Modifier.weight(1f)){Text(stringResource(R.string.retry))}
                }
            }}
            if(state.items.isEmpty()&&!state.loading)item{Text(stringResource(R.string.no_backups),color=Muted)}
            items(state.items,key={it.id}){artifact->Panel{
                Text(artifact.created?.let{parseTime(it,ZoneOffset.UTC)?.atZone(zone)?.format(DateTimeFormatter.ofPattern("dd.MM.yyyy HH:mm"))}?:artifact.id,style=MaterialTheme.typography.titleMedium)
                Text(Formatter.formatShortFileSize(context,artifact.bytes),color=Muted,style=MaterialTheme.typography.bodySmall)
                Text(stringResource(when(artifact.validation){"passed"->R.string.backup_validated;"failed"->R.string.backup_validation_failed;else->R.string.not_checked}),color=if(artifact.validation=="failed")MaterialTheme.colorScheme.error else Muted,style=MaterialTheme.typography.bodySmall)
                Row(horizontalArrangement=Arrangement.spacedBy(8.dp)){
                    OutlinedButton(onClick={confirm="validate" to artifact.id},enabled=state.pending==null,modifier=Modifier.weight(1f).testTag("backup-verify-${artifact.id}")){Text(stringResource(R.string.verify_backup))}
                    OutlinedButton(onClick={confirm="delete" to artifact.id},enabled=state.pending==null&&artifact.canDelete,modifier=Modifier.weight(1f).testTag("backup-delete-${artifact.id}")){Text(stringResource(R.string.remove))}
                }
            }}
            item{OutlinedButton(onClick={controller.refresh()},enabled=!state.loading,modifier=Modifier.fillMaxWidth()){Text(stringResource(R.string.refresh))}}
            if(state.more)item{OutlinedButton(onClick={controller.refresh(true)},enabled=!state.loading,modifier=Modifier.fillMaxWidth()){Text(stringResource(R.string.load_more))}}
        }
    }
    confirm?.let{(kind,artifact)->AlertDialog(onDismissRequest={confirm=null},modifier=Modifier.semantics{testTagsAsResourceId=true},text={Text(stringResource(when(kind){"create"->R.string.create_backup_confirmation;"validate"->R.string.verify_backup_confirmation;else->R.string.delete_backup_confirmation}))},
        confirmButton={TextButton(onClick={confirm=null;controller.start(kind,artifact)},modifier=Modifier.testTag("confirm-backup-action")){Text(stringResource(R.string.continue_action))}},
        dismissButton={TextButton(onClick={confirm=null}){Text(stringResource(R.string.cancel))}})}
}
