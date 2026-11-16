package ru.kmvms.mobile.ui.camera

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.clickable
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.res.painterResource
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.repeatOnLifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import kotlinx.coroutines.delay
import org.json.JSONObject
import ru.kmvms.mobile.R
import ru.kmvms.mobile.MobileState
import ru.kmvms.mobile.MobileViewModel
import ru.kmvms.mobile.camera.CameraApi
import ru.kmvms.mobile.camera.CameraCardStatus
import ru.kmvms.mobile.camera.cameraCardStatus
import ru.kmvms.mobile.data.Camera
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.Outline
import java.util.UUID

@Composable fun CameraManagementScreen(api:NasApi,state:MobileState,model:MobileViewModel,message:(String)->Unit,
                                      openLive: ((Camera) -> Unit)? = null) {
    var editing by remember(api){mutableStateOf(false)}
    var editedId by remember(api){mutableStateOf<Long?>(null)}
    var removed by remember(api){mutableStateOf<Camera?>(null)}
    val previews = remember(api) { CameraCardPreviews(api) }
    var previewRevision by remember(api) { mutableIntStateOf(0) }
    var recorder by remember(api) { mutableStateOf<JSONObject?>(null) }
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    LaunchedEffect(api, state.cameras, editing, lifecycle) {
        if (editing || state.user?.can("run_diagnostics") != true) return@LaunchedEffect
        lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
            do {
                recorder = try { api.json("api/system/recorder/summary") as JSONObject }
                    catch (cancelled: CancellationException) { throw cancelled }
                    catch (_: Exception) { null }
                if (state.cameras.none { it.enabled }) break
                delay(10_000) // UI-only freshness while this screen is actually visible.
            } while (true)
        }
    }
    if(editing) {
        CameraEditorScreen(api,editedId,{editing=false},{editing=false;previews.clear();previewRevision++;model.reloadCameras()},message)
        return
    }
    LazyColumn(Modifier.fillMaxSize(),contentPadding=PaddingValues(16.dp),verticalArrangement=Arrangement.spacedBy(12.dp)) {
        item{Row(Modifier.fillMaxWidth(),verticalAlignment=Alignment.CenterVertically){
            Text(stringResource(R.string.cameras),style=MaterialTheme.typography.titleLarge,modifier=Modifier.weight(1f))
            IconButton(onClick={editedId=null;editing=true},modifier=Modifier.testTag("add-camera")){Icon(Icons.Default.Add,stringResource(R.string.add_camera))}
            IconButton(onClick=model::reloadCameras,enabled=!state.loading){Icon(Icons.Default.Refresh,stringResource(R.string.refresh))}
        }}
        if(state.cameras.isEmpty())item{Text(stringResource(if(state.loading)R.string.loading else R.string.no_cameras),color=Muted)}
        items(state.cameras,key={it.id}){camera->Surface(shape=RoundedCornerShape(14.dp),color=Color.White,border=BorderStroke(1.dp,Outline)) {
            BoxWithConstraints(Modifier.padding(start=14.dp,end=14.dp,top=14.dp,bottom=4.dp).testTag("camera-card-${camera.id}")) {
                val previewWidth = (maxWidth * .42f).coerceIn(88.dp, 148.dp)
                Column {
                    Row(Modifier.fillMaxWidth(),horizontalArrangement=Arrangement.spacedBy(12.dp),verticalAlignment=Alignment.CenterVertically) {
                        Column(Modifier.weight(1f),verticalArrangement=Arrangement.spacedBy(4.dp)) {
                            Text(camera.name,style=MaterialTheme.typography.titleMedium,fontWeight=FontWeight.SemiBold,maxLines=2,overflow=TextOverflow.Ellipsis)
                            val status = cameraCardStatus(camera, recorder)
                            val statusColor = when(status) {
                                CameraCardStatus.RECORDING -> Color(0xFF329B68)
                                CameraCardStatus.STOPPED -> Color(0xFFD86363)
                                CameraCardStatus.STARTING -> Color(0xFFC28A31)
                                CameraCardStatus.UNKNOWN -> Muted
                            }
                            Row(Modifier.height(18.dp).testTag("camera-status-${camera.id}"),verticalAlignment=Alignment.CenterVertically,horizontalArrangement=Arrangement.spacedBy(6.dp)) {
                                Canvas(Modifier.size(7.dp)) { drawCircle(statusColor) }
                                Text(stringResource(when(status) {
                                    CameraCardStatus.RECORDING -> R.string.camera_recording_now
                                    CameraCardStatus.STOPPED -> R.string.camera_not_recording
                                    CameraCardStatus.STARTING -> R.string.camera_recording_starting
                                    CameraCardStatus.UNKNOWN -> R.string.camera_recording_unknown
                                }),color=Muted,style=MaterialTheme.typography.labelSmall,maxLines=1,overflow=TextOverflow.Ellipsis)
                            }
                            Text(listOf(camera.host,camera.protocol.uppercase()).filter{it.isNotBlank()}.joinToString(" · "),color=Muted,style=MaterialTheme.typography.bodySmall,maxLines=1,overflow=TextOverflow.Ellipsis)
                            Spacer(Modifier.height(2.dp))
                            camera.segmentMinutes?.let{Text(stringResource(R.string.segment_minutes,it),style=MaterialTheme.typography.bodySmall)}
                            camera.retentionDays?.let{Text(stringResource(R.string.retention_days,it),style=MaterialTheme.typography.bodySmall)}
                            camera.quotaGb?.let{Text(stringResource(R.string.quota_gb,it),style=MaterialTheme.typography.bodySmall)}
                        }
                        val previewAction = if (openLive != null) Modifier.clickable(
                            onClickLabel = stringResource(R.string.live_open_camera)) { openLive(camera) } else Modifier
                        key(previewRevision) { CameraCardPreview(previews,camera.id,Modifier.width(previewWidth).aspectRatio(16f/9f).then(previewAction)) }
                    }
                    Row(Modifier.fillMaxWidth(),verticalAlignment=Alignment.CenterVertically,horizontalArrangement=Arrangement.End) {
                        IconButton(onClick={removed=camera},modifier=Modifier.testTag("delete-camera-${camera.id}")) {
                            Icon(painterResource(R.drawable.ic_camera_trash),stringResource(R.string.remove),tint=Color(0xFFD85C63),modifier=Modifier.size(21.dp))
                        }
                        IconButton(onClick={editedId=camera.id;editing=true},modifier=Modifier.testTag("edit-camera-${camera.id}")) {
                            Icon(painterResource(R.drawable.ic_camera_edit),stringResource(R.string.edit_camera),tint=Muted,modifier=Modifier.size(21.dp))
                        }
                        CompactRecordingSwitch(camera.enabled,state.cameraAction==null,Modifier.testTag("recording-${camera.id}")){model.setRecording(camera)}
                    }
                }
            }
        }}
    }
    removed?.let{camera->CameraDeleteDialog(CameraApi(api),camera,state.user?.can("delete_recordings")==true,
        {removed=null},{removed=null;model.reloadCameras()},message)}
}

@Composable private fun CompactRecordingSwitch(checked:Boolean,enabled:Boolean,modifier:Modifier,onClick:()->Unit) {
    val track = if(checked)Color(0xFFDBEAFE) else Color(0xFFE2E8F0)
    val thumb = if(checked)Color(0xFF3B82F6) else Color(0xFF94A3B8)
    Box(modifier.size(48.dp).toggleable(checked,enabled=enabled,role=Role.Switch,onValueChange={onClick()}),contentAlignment=Alignment.Center) {
        Canvas(Modifier.size(36.dp,22.dp)) {
            val opacity=if(enabled)1f else .45f
            drawRoundRect(track.copy(alpha=opacity),cornerRadius=CornerRadius(size.height/2))
            drawRoundRect(thumb.copy(alpha=.25f*opacity),cornerRadius=CornerRadius(size.height/2),style=Stroke(1.dp.toPx()))
            drawCircle(thumb.copy(alpha=opacity),radius=8.dp.toPx(),center=Offset(if(checked)size.width-11.dp.toPx() else 11.dp.toPx(),size.height/2))
        }
    }
}

@Composable private fun CameraDeleteDialog(api:CameraApi,camera:Camera,canDeleteFiles:Boolean,dismiss:()->Unit,deleted:()->Unit,message:(String)->Unit) {
    var deleteFiles by remember{mutableStateOf(false)}
    var preview by remember{mutableStateOf<JSONObject?>(null)}
    var checking by remember{mutableStateOf(true)}
    var busy by remember{mutableStateOf(false)}
    val operation=remember(camera.id,deleteFiles){UUID.randomUUID().toString()}
    val scope=rememberCoroutineScope()
    val failure=stringResource(R.string.camera_action_failed)
    LaunchedEffect(camera.id,deleteFiles){
        checking=true;preview=null
        try{preview=api.deletePreview(camera.id,deleteFiles)}catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){message(failure)}finally{checking=false}
    }
    val blocked=deleteFiles&&preview?.optJSONObject("recordings")?.optBoolean("ok")!=true
    AlertDialog(onDismissRequest={if(!busy)dismiss()},modifier=Modifier.semantics{testTagsAsResourceId=true},
        title={Text(stringResource(R.string.delete_camera_title,camera.name))},
        text={Column(Modifier.fillMaxWidth().heightIn(min=170.dp),verticalArrangement=Arrangement.spacedBy(12.dp)){
            Text(stringResource(R.string.delete_camera_message))
            Row(verticalAlignment=Alignment.CenterVertically){
                Checkbox(deleteFiles,{deleteFiles=it},enabled=canDeleteFiles&&!busy&&!checking,modifier=Modifier.testTag("camera-delete-files"))
                Text(stringResource(R.string.delete_all_camera_recordings),modifier=Modifier.weight(1f))
            }
            Box(Modifier.fillMaxWidth().height(48.dp),contentAlignment=Alignment.CenterStart){
                if(checking||busy)LinearProgressIndicator(Modifier.fillMaxWidth())
                else if(blocked)Text(stringResource(R.string.camera_archive_delete_blocked),color=MaterialTheme.colorScheme.error,style=MaterialTheme.typography.bodySmall)
            }
        }},
        confirmButton={TextButton(onClick={
            if(busy||checking||blocked)return@TextButton
            busy=true
            scope.launch{try{
                val result=api.delete(camera.id,deleteFiles,operation)
                if(result.optBoolean("camera_removed")&&(!deleteFiles||result.optString("status")=="deleted"))deleted()else message(failure)
            }catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){message(failure)}finally{busy=false}}
        },enabled=preview!=null&&!checking&&!busy&&!blocked,modifier=Modifier.testTag("confirm-delete-camera")){Text(stringResource(R.string.remove))}},
        dismissButton={TextButton(onClick=dismiss,enabled=!busy){Text(stringResource(R.string.cancel))}})
}
