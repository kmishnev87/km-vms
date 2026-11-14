package ru.kmvms.mobile.ui.server

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.server.*
import ru.kmvms.mobile.ui.Muted

@Composable fun SystemScreen(api:NasApi,message:(String)->Unit) {
    val service=remember(api){ServerApi(api)}
    var refresh by remember{mutableIntStateOf(0)}
    var loading by remember{mutableStateOf(true)}
    var data by remember(api){mutableStateOf<SystemView?>(null)}
    val failure=stringResource(R.string.server_error)
    LaunchedEffect(service,refresh){loading=true;try{data=service.system()}catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){message(failure)}finally{loading=false}}
    Column(Modifier.fillMaxSize()){
        ServerHeader(R.string.system_status,loading){refresh++}
        val snapshot=data
        LazyColumn(Modifier.fillMaxSize().testTag("system-content"),contentPadding=PaddingValues(16.dp),verticalArrangement=Arrangement.spacedBy(12.dp)){
            if(snapshot!=null){
                item{Panel{
                    Text(stringResource(severityLabel(snapshot.severity)),style=MaterialTheme.typography.headlineSmall,color=severityColor(snapshot.severity))
                    Text(stringResource(R.string.system_counts,snapshot.problems,snapshot.warnings,snapshot.unknown),style=MaterialTheme.typography.bodyMedium,color=Muted)
                }}
                items(snapshot.domains,key={it.key}){domain->
                    var expanded by remember(domain.key){mutableStateOf(false)}
                    Panel{
                        Text(stringResource(domainLabel(domain.key)),style=MaterialTheme.typography.titleMedium)
                        if(domain.idle)Text(stringResource(R.string.no_active_live_views),color=Muted,style=MaterialTheme.typography.bodySmall)
                        else if(domain.severity!="ok")Text(stringResource(severityLabel(domain.severity)),color=severityColor(domain.severity),style=MaterialTheme.typography.bodySmall)
                        if(domain.issues.isNotEmpty()||domain.reasons.isNotEmpty())TextButton(onClick={expanded=!expanded}){Text(stringResource(if(expanded)R.string.hide_details else R.string.details))}
                        if(expanded){
                            domain.issues.forEach{issue->
                                issue.camera?.let{Text(it,style=MaterialTheme.typography.bodyMedium)}
                                issue.reasons.forEach{reason->Text(reasonText(reason),style=MaterialTheme.typography.bodySmall,color=Muted)}
                            }
                            domain.reasons.forEach{reason->Text(reasonText(reason),style=MaterialTheme.typography.bodySmall,color=Muted)}
                        }
                    }
                }
            }
        }
    }
}
@Composable private fun reasonText(code:String):String {
    val resource=when(code){
    "camera_disabled","recording_disabled"->R.string.recording_off
    "camera_unreachable","camera_offline"->R.string.camera_unreachable
    "storage_unavailable","storage_root_missing","archive_root_unavailable"->R.string.storage_unavailable
    "low_disk_space","critical_disk_space"->R.string.low_disk_space
    "no_evidence","not_checked","no_recent_check"->R.string.not_checked
    "recorder_heartbeat_stale","recorder_unavailable"->R.string.recorder_unavailable
    "missing_file","archive_problems"->R.string.archive_needs_attention
    else->R.string.server_attention_reason
    }
    return if(resource==R.string.server_attention_reason)stringResource(resource,code)else stringResource(resource)
}
private fun domainLabel(key:String)=when(key){"cameras"->R.string.cameras;"live"->R.string.live;"recorder"->R.string.recordings;"storage"->R.string.storage;"retention"->R.string.retention_rules;else->R.string.archive_integrity}
private fun severityLabel(value:String)=when(value){"ok"->R.string.system_operational;"error"->R.string.needs_attention;"warning"->R.string.system_warning;else->R.string.not_checked}
@Composable private fun severityColor(value:String)=when(value){"error"->MaterialTheme.colorScheme.error;"warning"->MaterialTheme.colorScheme.tertiary;else->Muted}
