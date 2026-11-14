package ru.kmvms.mobile.ui.camera

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import ru.kmvms.mobile.R
import ru.kmvms.mobile.camera.*
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.ui.Muted
import org.json.JSONObject

@Composable fun CameraEditorScreen(api:NasApi,id:Long?,close:()->Unit,saved:()->Unit,message:(String)->Unit) {
    val controller=remember(api,id){CameraEditorController(CameraApi(api),id)}
    val state by controller.state.collectAsStateWithLifecycle()
    var encoding by remember{mutableStateOf<Pair<String,JSONObject>?>(null)}
    val failure=stringResource(R.string.camera_action_failed)
    LaunchedEffect(state.failures){if(state.failures>0)message(failure)}
    LaunchedEffect(state.saved){if(state.saved)saved()}
    DisposableEffect(controller){onDispose{controller.close()}}
    BackHandler { close() }
    Column(Modifier.fillMaxSize().imePadding()) {
        Row(Modifier.fillMaxWidth(),verticalAlignment=Alignment.CenterVertically){
            IconButton(onClick=close){Icon(Icons.AutoMirrored.Filled.ArrowBack,stringResource(R.string.back))}
            Text(stringResource(if(id==null)R.string.add_camera else R.string.edit_camera),style=MaterialTheme.typography.titleLarge)
        }
        Row(Modifier.fillMaxWidth().padding(horizontal=16.dp),horizontalArrangement=Arrangement.spacedBy(6.dp)) {
            listOf(R.string.connection,R.string.video_streams,R.string.recording_settings).forEachIndexed{index,label->
                TextButton(onClick={controller.phase(index)},enabled=!state.busy&&(id!=null||index==0||state.verified),modifier=Modifier.weight(1f).testTag("camera-step-$index")) {
                    Text(stringResource(label),color=if(state.phase==index)MaterialTheme.colorScheme.primary else Muted)
                }
            }
        }
        Box(Modifier.fillMaxWidth().height(6.dp)){if(state.busy)LinearProgressIndicator(Modifier.fillMaxWidth())}
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(16.dp),verticalArrangement=Arrangement.spacedBy(12.dp)) {
            when(state.phase) {
                0->{
                    CameraField(R.string.camera_name,"name",state,controller)
                    ChoiceField(stringResource(R.string.protocol),state.draft["protocol"],listOf("onvif","rtsp"),!state.busy,"camera-protocol"){controller.change("protocol",it)}
                    Row(horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                        CameraField(R.string.camera_address,"host",state,controller,Modifier.weight(2f))
                        CameraField(R.string.port,"port",state,controller,Modifier.weight(1f),numeric=true)
                    }
                    CameraField(R.string.username,"username",state,controller)
                    CameraField(if(id==null)R.string.password else R.string.password_unchanged,"password",state,controller,password=true)
                    if(state.draft["protocol"]=="rtsp") {
                        CameraField(R.string.main_path,"rtsp_main_url",state,controller)
                        CameraField(R.string.sub_path,"rtsp_sub_url",state,controller)
                    } else {
                        var advanced by remember{mutableStateOf(false)}
                        TextButton(onClick={advanced=!advanced}){Text(stringResource(R.string.connection_advanced))}
                        if(advanced) {
                            CameraField(R.string.video_address,"rtsp_host",state,controller)
                            CameraField(R.string.video_port,"rtsp_port",state,controller,numeric=true)
                            CameraField(R.string.onvif_path,"onvif_path",state,controller)
                        }
                    }
                    OutlinedButton(onClick={controller.check()},enabled=!state.busy&&state.draft.validConnection(id!=null)&&state.draft.validPolicy(),modifier=Modifier.fillMaxWidth().testTag("camera-check")){
                        Text(stringResource(if(state.verified)R.string.check_again else R.string.check_connection))
                    }
                }
                1->{
                    val catalog=state.catalog
                    if(state.draft["protocol"]=="onvif")OutlinedButton(onClick={controller.check()},enabled=!state.busy,modifier=Modifier.fillMaxWidth().testTag("camera-read-profiles")){Text(stringResource(R.string.read_streams))}
                    if(catalog!=null) {
                        if(catalog.sources.size>1) {
                            val labels=catalog.sources.mapIndexed{index,source->source.id to stringResource(R.string.camera_lens,index+1)}
                            LabeledChoice(stringResource(R.string.camera_lens_label),labels.find{it.first==state.source}?.second.orEmpty(),labels,!state.busy,"camera-lens"){token->catalog.sources.find{it.id==token}?.let(controller::source)}
                        }
                        val group=catalog.sources.find{it.id==state.source}
                        val profiles=catalog.profiles.filter{group==null||it.token in group.profiles}
                        listOf("record" to R.string.record_stream,"live" to R.string.live_stream).forEach{(purpose,label)->
                            val role=state.draft["default_${purpose}_stream"]
                            val token=state.draft[if(role=="main")"onvif_profile_token"else"onvif_sub_profile_token"]
                            val choices=profiles.mapIndexed{index,profile->profile.token to (stringResource(R.string.stream_number,index+1)+profile.description().takeIf{it.isNotBlank()}?.let{"\n$it"}.orEmpty())}
                            LabeledChoice(stringResource(label),choices.find{it.first==token}?.second?:stringResource(R.string.choose_stream),choices,!state.busy,"camera-$purpose-profile"){selected->profiles.find{it.token==selected}?.let{controller.purpose(purpose,it)}}
                            state.configurations[token]?.let{config->TextButton(onClick={encoding=token to config},enabled=!state.busy,modifier=Modifier.testTag("camera-$purpose-encoding")){Text(stringResource(R.string.camera_encoding))}}
                        }
                    }
                    if(state.draft["protocol"]=="rtsp") {
                        CameraField(R.string.main_path,"rtsp_main_url",state,controller)
                        CameraField(R.string.sub_path,"rtsp_sub_url",state,controller)
                    }
                    if(state.eventsSupported==true||state.draft.flag("events_enabled"))Row(verticalAlignment=Alignment.CenterVertically){
                        Text(stringResource(R.string.receive_camera_events),modifier=Modifier.weight(1f))
                        Switch(checked=state.draft.flag("events_enabled"),onCheckedChange={controller.change("events_enabled",it.toString())},enabled=!state.busy)
                    }
                }
                2->{
                    Row(verticalAlignment=Alignment.CenterVertically){Text(stringResource(R.string.enable_recording),modifier=Modifier.weight(1f));Switch(state.draft.flag("enabled"),{controller.change("enabled",it.toString())},enabled=!state.busy,modifier=Modifier.testTag("camera-enabled"))}
                    CameraField(R.string.camera_name,"name",state,controller)
                    CameraField(R.string.segment_duration,"segment_minutes",state,controller,numeric=true)
                    CameraField(R.string.retention_duration,"retention_days",state,controller,numeric=true)
                    CameraField(R.string.camera_quota,"storage_quota_gb",state,controller,numeric=true)
                }
            }
        }
        Row(Modifier.fillMaxWidth().padding(16.dp),horizontalArrangement=Arrangement.spacedBy(8.dp)) {
            if(state.phase>0)OutlinedButton(onClick={controller.phase(state.phase-1)},enabled=!state.busy,modifier=Modifier.weight(1f)){Text(stringResource(R.string.back))}
            if(state.phase<2)Button(onClick=controller::next,enabled=!state.busy&&state.draft.validConnection(id!=null)&&state.draft.validPolicy(),modifier=Modifier.weight(1f).testTag("camera-next")){Text(stringResource(R.string.next))}
            else Button(onClick={controller.save()},enabled=!state.busy&&state.draft.validPolicy(),modifier=Modifier.weight(1f).testTag("camera-save")){Text(stringResource(R.string.save))}
        }
    }
    encoding?.let{(token,config)->CameraEncodingDialog(config,{encoding=null}){diff->controller.applyConfiguration(token,diff)}}
}

@Composable private fun CameraField(label:Int,key:String,state:CameraEditorState,controller:CameraEditorController,
                                   modifier:Modifier=Modifier,numeric:Boolean=false,password:Boolean=false) {
    OutlinedTextField(state.draft[key],{controller.change(key,it)},label={Text(stringResource(label))},enabled=!state.busy,singleLine=true,
        visualTransformation=if(password)PasswordVisualTransformation()else VisualTransformation.None,
        keyboardOptions=KeyboardOptions(keyboardType=when{password->KeyboardType.Password;numeric->KeyboardType.Number;else->KeyboardType.Text}),
        modifier=modifier.fillMaxWidth().testTag("camera-field-$key"))
}
@Composable fun ChoiceField(label:String,current:String,options:List<String>,enabled:Boolean,tag:String,choose:(String)->Unit)=
    LabeledChoice(label,current.uppercase(),options.map{it to it.uppercase()},enabled,tag,choose)
@Composable fun LabeledChoice(label:String,current:String,options:List<Pair<String,String>>,enabled:Boolean,tag:String,choose:(String)->Unit) {
    var open by remember{mutableStateOf(false)}
    Column(verticalArrangement=Arrangement.spacedBy(5.dp)) {
        Text(label,style=MaterialTheme.typography.labelMedium,color=Muted)
        Box(Modifier.fillMaxWidth()) {
            OutlinedButton(onClick={open=true},enabled=enabled,modifier=Modifier.fillMaxWidth().heightIn(min=48.dp).testTag(tag),shape=RoundedCornerShape(10.dp),contentPadding=PaddingValues(horizontal=14.dp,vertical=10.dp)){
                Text(current,modifier=Modifier.weight(1f),color=if(enabled)MaterialTheme.colorScheme.onSurface else Muted)
                Text("▾",color=Muted,modifier=Modifier.padding(start=8.dp))
            }
            DropdownMenu(open,{open=false},modifier=Modifier.heightIn(max=360.dp)){options.forEach{(value,text)->DropdownMenuItem(text={Text(text)},onClick={open=false;choose(value)})}}
        }
    }
}
