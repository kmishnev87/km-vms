package ru.kmvms.mobile.ui.server

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.data.NasUser
import ru.kmvms.mobile.server.*
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.camera.LabeledChoice
import java.time.ZoneId
import java.time.Instant
import ru.kmvms.mobile.core.gmtTimezones
import ru.kmvms.mobile.core.gmtTimezoneLabel

@Composable fun ServerSettingsScreen(api:NasApi,namespace:String,actor:NasUser,credentialsChanged:()->Unit,close:()->Unit,message:(String)->Unit) {
    val service=remember(api){ServerApi(api)}
    var baseline by remember(api){mutableStateOf<ServerSettings?>(null)}
    var edited by remember(api){mutableStateOf<ServerSettings?>(null)}
    var backends by remember{mutableStateOf(listOf("auto","cpu"))}
    var loading by remember{mutableStateOf(true)}
    var saving by remember{mutableStateOf(false)}
    var acknowledged by remember{mutableStateOf(false)}
    var confirmCleanup by remember{mutableStateOf(false)}
    var refresh by remember{mutableIntStateOf(0)}
    var backups by rememberSaveable{mutableStateOf(false)}
    var updates by rememberSaveable{mutableStateOf(false)}
    var usersOpen by remember(api){mutableStateOf(false)}
    val scope=rememberCoroutineScope()
    val failure=stringResource(R.string.server_error)
    val saved=stringResource(R.string.settings_saved)
    val compact=LocalConfiguration.current.screenHeightDp<500
    BackHandler{close()}
    LaunchedEffect(service,refresh){
        loading=true
        try{val settings=service.settings();baseline=settings;edited=settings;acknowledged=false
            backends=try{service.hardware()}catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){listOf("auto","cpu")}
        }catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){message(failure)}finally{loading=false}
    }
    if(backups){BackupsScreen(service,namespace,baseline?.timezone?:"UTC",{backups=false},message);return}
    if(updates){NasUpdatesScreen(api,namespace,{updates=false},message);return}
    if(usersOpen && UserManagementPolicy.allowed(actor)) {
        BackHandler { usersOpen=false }
        UsersScreen(api,actor,{usersOpen=false},credentialsChanged,message);return
    }
    Column(Modifier.fillMaxSize()){
        Row(verticalAlignment=Alignment.CenterVertically){
            IconButton(onClick=close){Icon(Icons.AutoMirrored.Filled.ArrowBack,stringResource(R.string.back))}
            Text(stringResource(R.string.server_settings),style=MaterialTheme.typography.titleLarge)
        }
        Box(Modifier.fillMaxWidth().height(6.dp)){if(loading||saving)LinearProgressIndicator(Modifier.fillMaxWidth())}
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(16.dp),verticalArrangement=Arrangement.spacedBy(14.dp)){
            val value=edited
            if(value!=null) {
                Panel{
                    Text(stringResource(R.string.general_settings),style=MaterialTheme.typography.titleMedium)
                    val languages=listOf("en" to "English","ru" to "Русский","zh-CN" to "中文")
                    SettingsFields(first={
                        LabeledChoice(stringResource(R.string.nas_language),languages.find{it.first==value.language}?.second?:value.language,languages,!loading&&!saving,"nas-language"){edited=value.copy(language=it)}
                    },second={
                        LabeledChoice(stringResource(R.string.timezone),gmtTimezoneLabel(value.timezone),gmtTimezones.map{it.id to it.label},!loading&&!saving,"nas-timezone"){edited=value.copy(timezone=it)}
                    })
                }
                Panel{
                    Text(stringResource(R.string.recording_settings),style=MaterialTheme.typography.titleMedium)
                    val formats=listOf("mkv" to "MKV","mp4" to "MP4")
                    val labels=(backends+value.backend).distinct().map{it to when(it){"auto"->stringResource(R.string.automatic);"cpu"->stringResource(R.string.cpu_only);else->it.uppercase()}}
                    SettingsFields(first={LabeledChoice(stringResource(R.string.recording_format),value.format.uppercase(),formats,!loading&&!saving,"nas-recording-format"){edited=value.copy(format=it)}},
                        second={LabeledChoice(stringResource(R.string.hardware_acceleration),labels.find{it.first==value.backend}?.second.orEmpty(),labels,!loading&&!saving,"nas-hardware"){edited=value.copy(backend=it)}})
                    Text(stringResource(R.string.format_next_segment),color=Muted,style=MaterialTheme.typography.bodySmall)
                }
                Panel{
                    Text(stringResource(R.string.low_disk_protection),style=MaterialTheme.typography.titleMedium)
                    Row(verticalAlignment=Alignment.CenterVertically){
                        Text(stringResource(R.string.low_disk_cleanup),modifier=Modifier.weight(1f))
                        Switch(value.autoCleanup,{enabled->
                            if(enabled&&baseline?.acknowledgementRequired==true&&!acknowledged)confirmCleanup=true else edited=value.copy(autoCleanup=enabled)
                        },enabled=!loading&&!saving,modifier=Modifier.testTag("nas-low-disk-cleanup"))
                    }
                }
                if(UserManagementPolicy.allowed(actor)) OutlinedButton(onClick={usersOpen=true},enabled=!loading&&!saving,modifier=Modifier.fillMaxWidth().height(48.dp).testTag("open-users")){Text(stringResource(R.string.users_and_roles))}
                OutlinedButton(onClick={backups=true},enabled=!loading&&!saving,modifier=Modifier.fillMaxWidth().height(48.dp).testTag("open-backups")){Text(stringResource(R.string.backups))}
                OutlinedButton(onClick={updates=true},enabled=!loading&&!saving,modifier=Modifier.fillMaxWidth().height(48.dp).testTag("open-nas-updates")){Text(stringResource(R.string.nas_updates))}
            }
        }
        Row(Modifier.widthIn(max=440.dp).align(Alignment.End).fillMaxWidth().padding(horizontal=16.dp,vertical=if(compact)8.dp else 16.dp),horizontalArrangement=Arrangement.spacedBy(8.dp)){
            OutlinedButton(onClick={refresh++},enabled=!loading&&!saving,modifier=Modifier.weight(1f)){Text(stringResource(R.string.refresh))}
            Button(onClick={val value=edited?:return@Button;val previous=baseline?:return@Button;saving=true;scope.launch{
                try{val result=service.save(value,previous,acknowledged);baseline=result;edited=result;message(saved)}catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){message(failure)}finally{saving=false}
            }},enabled=!loading&&!saving&&edited!=null&&edited!=baseline,modifier=Modifier.weight(1f).testTag("nas-settings-save")){Text(stringResource(R.string.save))}
        }
    }
    if(confirmCleanup)AlertDialog(onDismissRequest={confirmCleanup=false},title={Text(stringResource(R.string.low_disk_protection))},text={Text(stringResource(R.string.low_disk_cleanup_agreement))},
        confirmButton={TextButton(onClick={acknowledged=true;edited=edited?.copy(autoCleanup=true);confirmCleanup=false}){Text(stringResource(R.string.agree))}},
        dismissButton={TextButton(onClick={confirmCleanup=false}){Text(stringResource(R.string.cancel))}})
}
@Composable
private fun SettingsFields(first: @Composable () -> Unit, second: @Composable () -> Unit) {
    BoxWithConstraints(Modifier.fillMaxWidth()) {
        if (maxWidth >= 600.dp) {
            Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                Box(Modifier.weight(1f)) { first() }
                Box(Modifier.weight(1f)) { second() }
            }
        } else {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                first()
                second()
            }
        }
    }
}
