package ru.kmvms.mobile.ui.server

import android.text.format.Formatter
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.server.*
import ru.kmvms.mobile.ui.Muted

@Composable fun StorageScreen(api:NasApi,namespace:String,canMaintain:Boolean,message:(String)->Unit) {
    val service=remember(api){ServerApi(api)}
    var refresh by remember{mutableIntStateOf(0)}
    var loading by remember{mutableStateOf(true)}
    var data by remember(api){mutableStateOf<StorageView?>(null)}
    var maintenance by remember{mutableStateOf(false)}
    val context=LocalContext.current
    val failure=stringResource(R.string.server_error)
    LaunchedEffect(service,refresh){loading=true;try{data=service.storage()}catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){message(failure)}finally{loading=false}}
    if(maintenance){ArchiveMaintenanceScreen(api,namespace,{maintenance=false;refresh++},message);return}
    Column(Modifier.fillMaxSize()) {
        ServerHeader(R.string.storage,loading){refresh++}
        val snapshot=data
        LazyColumn(Modifier.fillMaxSize().testTag("storage-content"),contentPadding=PaddingValues(16.dp),verticalArrangement=Arrangement.spacedBy(12.dp)){
            if(snapshot!=null){
                if(canMaintain)item{OutlinedButton(onClick={maintenance=true},modifier=Modifier.fillMaxWidth().height(48.dp).testTag("open-archive-maintenance")){Text(stringResource(R.string.archive_maintenance))}}
                item{Panel{
                    Text(stringResource(R.string.archive_and_space),style=MaterialTheme.typography.titleMedium)
                    Text(Formatter.formatShortFileSize(context,snapshot.archiveBytes),style=MaterialTheme.typography.headlineMedium,fontWeight=FontWeight.SemiBold)
                    Text(stringResource(R.string.archive_used),color=Muted,style=MaterialTheme.typography.labelMedium)
                    if(snapshot.available&&snapshot.total!=null&&snapshot.free!=null&&snapshot.total>0) {
                        LinearProgressIndicator(progress={((snapshot.total-snapshot.free).toDouble()/snapshot.total).toFloat().coerceIn(0f,1f)},modifier=Modifier.fillMaxWidth())
                        Text(stringResource(R.string.disk_space,Formatter.formatShortFileSize(context,snapshot.free),Formatter.formatShortFileSize(context,snapshot.total)),color=Muted,style=MaterialTheme.typography.bodySmall)
                    }else Text(stringResource(if(snapshot.available)R.string.capacity_unavailable else R.string.storage_unavailable),color=if(snapshot.available)Muted else MaterialTheme.colorScheme.error)
                    if(snapshot.problems>0)Text(stringResource(R.string.archive_problem_count,snapshot.problems),color=MaterialTheme.colorScheme.error)
                }}
                item{Panel{
                    Text(stringResource(R.string.cameras),style=MaterialTheme.typography.titleMedium)
                    val ordered = snapshot.cameras.sortedBy { it.deleted }
                    Column(Modifier.fillMaxWidth().testTag("storage-camera-list")) {
                        ordered.forEachIndexed { index, camera ->
                            if(camera.deleted && ordered.getOrNull(index-1)?.deleted != true) {
                                HorizontalDivider(Modifier.padding(top=5.dp,bottom=4.dp))
                                Text(stringResource(R.string.retained_archive_cameras),color=Muted,style=MaterialTheme.typography.labelSmall,modifier=Modifier.padding(bottom=3.dp).testTag("storage-retained-divider"))
                            }
                            Row(Modifier.fillMaxWidth().height(36.dp).testTag("storage-camera-${camera.id}"),verticalAlignment=Alignment.CenterVertically,horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                                Row(Modifier.weight(1f),verticalAlignment=Alignment.CenterVertically) {
                                    Text(camera.name,modifier=Modifier.weight(1f,false),maxLines=1,overflow=TextOverflow.Ellipsis,style=MaterialTheme.typography.bodySmall)
                                    if(camera.deleted)Text(" (${stringResource(R.string.deleted_camera)})",style=MaterialTheme.typography.labelSmall,color=Muted)
                                }
                                Text(stringResource(R.string.camera_archive_usage,Formatter.formatShortFileSize(context,camera.bytes),camera.count),maxLines=1,style=MaterialTheme.typography.labelSmall,color=Muted)
                            }
                        }
                    }
                }}
                item{Text(stringResource(R.string.archive_roots),style=MaterialTheme.typography.titleMedium)}
                items(snapshot.roots,key={it.id}){root->Panel{
                    Row(verticalAlignment=Alignment.CenterVertically){
                        Text(root.label.ifBlank{stringResource(R.string.archive)},modifier=Modifier.weight(1f),fontWeight=FontWeight.Medium)
                        if(root.active)Text(stringResource(R.string.active_archive),style=MaterialTheme.typography.labelMedium,color=MaterialTheme.colorScheme.primary)
                    }
                    root.path?.let{Text(it,style=MaterialTheme.typography.bodySmall,color=Muted,maxLines=2,overflow=TextOverflow.Ellipsis)}
                    Text(Formatter.formatShortFileSize(context,root.bytes),style=MaterialTheme.typography.bodySmall,color=Muted)
                    if(!root.readable)Text(stringResource(if(root.requiresActivation&&!root.active)R.string.inactive_archive else R.string.storage_unavailable),style=MaterialTheme.typography.bodySmall,color=if(root.requiresActivation&&!root.active)Muted else MaterialTheme.colorScheme.error)
                }}
            }
        }
    }
}
@Composable internal fun ServerHeader(title:Int,loading:Boolean,refresh:()->Unit) {
    Column {
        Row(Modifier.fillMaxWidth().padding(start=16.dp,end=8.dp),verticalAlignment=Alignment.CenterVertically){
            Text(stringResource(title),style=MaterialTheme.typography.titleLarge,modifier=Modifier.weight(1f))
            IconButton(onClick=refresh,enabled=!loading,modifier=Modifier.testTag("server-refresh")){Icon(Icons.Default.Refresh,stringResource(R.string.refresh))}
        }
        Box(Modifier.fillMaxWidth().height(6.dp)){if(loading)LinearProgressIndicator(Modifier.fillMaxWidth())}
    }
}
@Composable internal fun Panel(content:@Composable ColumnScope.()->Unit) {
    Surface(shape=RoundedCornerShape(14.dp),color=Color.White,modifier=Modifier.fillMaxWidth()){
        Column(Modifier.padding(16.dp),verticalArrangement=Arrangement.spacedBy(10.dp),content=content)
    }
}
