package ru.kmvms.mobile.ui.server

import android.text.format.Formatter
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.server.*
import ru.kmvms.mobile.ui.Muted

@Composable fun ArchiveMaintenanceScreen(api:NasApi,namespace:String,close:()->Unit,message:(String)->Unit) {
    val context=LocalContext.current
    val controller=remember(api,namespace){ArchiveMaintenanceController(context,ArchiveMaintenanceApi(api),namespace)}
    val state by controller.state.collectAsStateWithLifecycle()
    var actionMenu by remember{mutableStateOf(false)}
    val lifecycle=LocalLifecycleOwner.current.lifecycle
    val failure=stringResource(R.string.server_error)
    BackHandler{close()}
    DisposableEffect(controller,lifecycle){
        controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED))
        val observer=LifecycleEventObserver{_,_->controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED))}
        lifecycle.addObserver(observer)
        onDispose{lifecycle.removeObserver(observer);controller.close()}
    }
    LaunchedEffect(state.failures){if(state.failures>0)message(failure)}
    Column(Modifier.fillMaxSize()){
        Row(verticalAlignment=Alignment.CenterVertically){
            IconButton(onClick=close){Icon(Icons.AutoMirrored.Filled.ArrowBack,stringResource(R.string.back))}
            Text(stringResource(R.string.archive_maintenance),style=MaterialTheme.typography.titleLarge,modifier=Modifier.weight(1f))
            IconButton(onClick={controller.refresh()},enabled=!state.loading){Icon(Icons.Default.Refresh,stringResource(R.string.refresh))}
        }
        Box(Modifier.height(6.dp).fillMaxWidth()){if(state.loading||state.busy||state.scan?.running==true)LinearProgressIndicator(Modifier.fillMaxWidth())}
        Row(Modifier.fillMaxWidth().padding(horizontal=16.dp),verticalAlignment=Alignment.CenterVertically,horizontalArrangement=Arrangement.spacedBy(8.dp)){
            val scan=state.scan
            Text(if(scan==null||scan.status=="not_run")stringResource(R.string.not_checked)
                else if(scan.stale)stringResource(R.string.archive_scan_stale)
                else if(scan.running)stringResource(R.string.archive_scan_progress,scan.checked,scan.found)
                else if(scan.status !in setOf("completed","partial"))stringResource(R.string.operation_failed)
                else stringResource(R.string.archive_problem_count,state.total),style=MaterialTheme.typography.bodySmall,color=Muted,modifier=Modifier.weight(1f))
            OutlinedButton(onClick=controller::startScan,enabled=state.pending==null&&state.batch==null&&state.scan?.running!=true&&!state.busy,modifier=Modifier.testTag("archive-start-scan")){Text(stringResource(R.string.archive_scan))}
        }
        Row(Modifier.fillMaxWidth().height(48.dp).padding(horizontal=4.dp),verticalAlignment=Alignment.CenterVertically){
            val all=state.items.isNotEmpty()&&state.items.all{it.id in state.selected}
            Checkbox(all,{value->state.items.forEach{controller.select(it.id,value)}},enabled=state.items.isNotEmpty()&&!state.busy)
            Text(stringResource(R.string.archive_selected,state.selected.size),modifier=Modifier.weight(1f),style=MaterialTheme.typography.bodySmall)
            Box{
                TextButton(onClick={actionMenu=true},enabled=state.selected.isNotEmpty()&&!state.busy&&state.pending==null,modifier=Modifier.testTag("archive-selected-actions")){Text(stringResource(R.string.archive_actions))}
                DropdownMenu(actionMenu,{actionMenu=false}){listOf("recheck","repair","acknowledge","delete").forEach{action->
                    if(state.items.any{it.id in state.selected&&action in it.actions})DropdownMenuItem(text={Text(integrityActionLabel(action))},onClick={actionMenu=false;controller.preview(action)})
                }}
            }
        }
        LazyColumn(Modifier.weight(1f).fillMaxWidth().testTag("archive-findings"),contentPadding=PaddingValues(horizontal=12.dp,vertical=4.dp),verticalArrangement=Arrangement.spacedBy(8.dp)){
            if(state.pending!=null)item{Panel{
                Text(stringResource(R.string.operation_unknown),color=Muted,style=MaterialTheme.typography.bodySmall)
                OutlinedButton(onClick=controller::retry,enabled=!state.busy){Text(stringResource(R.string.retry))}
            }}
            if(state.result!=null&&state.pending==null)item{Text(stringResource(when(state.result){"completed"->R.string.operation_completed;"unknown"->R.string.operation_unknown;"prepared","running","queued"->R.string.operation_running;else->R.string.operation_failed}),color=Muted,modifier=Modifier.padding(8.dp))}
            items(state.items,key={it.id}){finding->Surface(color=MaterialTheme.colorScheme.surface,shape=MaterialTheme.shapes.medium){
                Row(Modifier.fillMaxWidth().padding(end=12.dp,top=8.dp,bottom=8.dp),verticalAlignment=Alignment.CenterVertically){
                    Checkbox(finding.id in state.selected,{controller.select(finding.id,it)},enabled=!state.busy)
                    Column(Modifier.weight(1f),verticalArrangement=Arrangement.spacedBy(4.dp)){
                        Text(finding.camera,style=MaterialTheme.typography.titleSmall,maxLines=1,overflow=TextOverflow.Ellipsis)
                        Text(finding.filename,style=MaterialTheme.typography.bodySmall,maxLines=2,overflow=TextOverflow.Ellipsis,color=Muted)
                        Text(integrityCategoryLabel(finding.category),style=MaterialTheme.typography.labelSmall,color=MaterialTheme.colorScheme.error)
                    }
                }
            }}
            if(state.next!=null)item{OutlinedButton(onClick={controller.refresh(true)},enabled=!state.loading,modifier=Modifier.fillMaxWidth()){Text(stringResource(R.string.load_more))}}
        }
    }
    state.preview?.let{preview->AlertDialog(onDismissRequest=controller::dismissPreview,title={Text(integrityActionLabel(preview.action))},
        text={Column(verticalArrangement=Arrangement.spacedBy(8.dp)){
            Text(stringResource(R.string.archive_action_count,preview.eligible,preview.unavailable))
            if(preview.action=="delete")Text(stringResource(R.string.archive_delete_confirmation,Formatter.formatShortFileSize(context,preview.bytes)))
        }},confirmButton={TextButton(onClick=controller::confirm,enabled=preview.eligible>0,modifier=Modifier.testTag("confirm-archive-action")){Text(stringResource(R.string.continue_action))}},
        dismissButton={TextButton(onClick=controller::dismissPreview){Text(stringResource(R.string.cancel))}})}
}
@Composable private fun integrityActionLabel(action:String)=stringResource(when(action){"recheck"->R.string.archive_recheck;"repair"->R.string.archive_repair;"delete"->R.string.remove;else->R.string.archive_acknowledge})
@Composable private fun integrityCategoryLabel(category:String)=stringResource(when(category){
    "missing_file","orphan_metadata"->R.string.archive_missing_file
    "partial_file","stale_writing_segment"->R.string.archive_unfinished_file
    "corrupted_file","zero_size_file","unreadable_file"->R.string.archive_unreadable_file
    "orphan_file","pre_metadata_km_vms_file","legacy_archive_file"->R.string.archive_uncatalogued_file
    else->R.string.archive_item_needs_review
})
