package ru.kmvms.mobile.ui.live

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import ru.kmvms.mobile.R
import ru.kmvms.mobile.talk.CallPhase
import ru.kmvms.mobile.talk.TalkController

@Composable fun CameraCallLifecycle(talk: TalkController, selection: (Boolean, Boolean) -> Unit) {
    val state by talk.state.collectAsStateWithLifecycle()
    val capability by talk.capability.collectAsStateWithLifecycle()
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    LaunchedEffect(state.phase, state.microphone, capability.fullDuplex) {
        selection(state.phase in setOf(CallPhase.CONNECTING, CallPhase.ACTIVE), !capability.fullDuplex && state.microphone)
    }
    DisposableEffect(talk, lifecycle) {
        val observer = LifecycleEventObserver { _, event ->
            if (event in setOf(Lifecycle.Event.ON_PAUSE, Lifecycle.Event.ON_STOP)) { talk.end(); selection(false, false) }
        }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer); talk.close(); selection(false, false) }
    }
}

@Composable fun CallButton(talk: TalkController, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val capability by talk.capability.collectAsStateWithLifecycle()
    val state by talk.state.collectAsStateWithLifecycle()
    var credentials by remember { mutableStateOf(false) }
    var information by remember { mutableStateOf(false) }
    var denied by remember { mutableStateOf(false) }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) talk.start() else denied = true
    }
    LaunchedEffect(state.phase, state.reason) {
        if (capability.credentials && state.phase == CallPhase.FAILED &&
            state.reason in setOf("talk_authentication_failed", "talk_access_denied", "talk_credentials_required")) {
            talk.editCredential()
            credentials = true
        }
    }
    fun connect() {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) talk.start()
        else launcher.launch(Manifest.permission.RECORD_AUDIO)
    }
    IconButton(onClick = {
        when {
            state.phase in setOf(CallPhase.CONNECTING, CallPhase.ACTIVE) -> talk.end()
            !capability.available -> information = true
            capability.credentials && (!capability.saved || state.reason in setOf("talk_authentication_failed", "talk_access_denied", "talk_credentials_required")) -> {
                talk.editCredential(); credentials = true
            }
            else -> connect()
        }
    }, enabled = capability.status != "loading", modifier = modifier.testTag("live-call")) {
        PlayerGlyph(PlayerGlyph.CALL, when {
            state.phase == CallPhase.ACTIVE -> Color(0xFF60A5FA)
            capability.available -> Color.White
            else -> Color(0xFF94A3B8)
        }, stringResource(R.string.camera_call))
    }
    if (credentials) {
        var secret by remember { mutableStateOf("") }
        val credential by talk.credential.collectAsStateWithLifecycle()
        val saving = credential.saving
        val failure = credential.reason.ifBlank { if (state.phase == CallPhase.FAILED) state.reason else "" }
        AlertDialog(onDismissRequest = { if (!saving) { secret = ""; credentials = false } },
            title = { Text(stringResource(R.string.call_password)) },
            text = { Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(stringResource(R.string.call_password_help))
                OutlinedTextField(secret, { secret = it }, label = { Text(stringResource(R.string.call_password)) },
                    enabled = !saving, isError = failure.isNotBlank(), singleLine = true,
                    visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth().testTag("call-password"))
                if (failure.isNotBlank()) Text(stringResource(callReason(failure)), color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall, modifier = Modifier.testTag("call-password-error"))
                if (saving) LinearProgressIndicator(Modifier.fillMaxWidth().testTag("call-password-checking"))
            } },
            confirmButton = { TextButton(onClick = { talk.saveCredential(secret) { secret = ""; credentials = false; connect() } },
                modifier = Modifier.testTag("call-password-connect"), enabled = secret.isNotBlank() && !saving) { Text(stringResource(R.string.call_save_connect)) } },
            dismissButton = { TextButton(onClick = { secret = ""; credentials = false }, enabled = !saving) { Text(stringResource(R.string.cancel)) } })
    }
    if (information || denied) AlertDialog(onDismissRequest = { information = false; denied = false },
        title = { Text(stringResource(R.string.camera_call)) },
        text = { Text(stringResource(if (denied) R.string.call_permission else callReason(capability.reason))) },
        confirmButton = { TextButton(onClick = { information = false; denied = false; talk.reload() }) { Text(stringResource(R.string.close)) } })
}

@Composable fun CallStatus(talk: TalkController, modifier: Modifier = Modifier) {
    val state by talk.state.collectAsStateWithLifecycle()
    val capability by talk.capability.collectAsStateWithLifecycle()
    var modeMenu by remember { mutableStateOf(false) }
    if (state.phase == CallPhase.IDLE) return
    Row(modifier.background(Color(0xD9233044), RoundedCornerShape(12.dp)).padding(horizontal = 8.dp),
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        when (state.phase) {
            CallPhase.CONNECTING -> {
                CircularProgressIndicator(Modifier.size(18.dp), color = Color.White, strokeWidth = 2.dp)
                Text(stringResource(R.string.call_connecting), color = Color.White, style = MaterialTheme.typography.labelMedium)
            }
            CallPhase.ACTIVE -> if (capability.fullDuplex) {
                IconButton(onClick = { talk.microphone(!state.microphone) }, modifier = Modifier.testTag("call-microphone")) {
                    PlayerGlyph(if (state.microphone) PlayerGlyph.MIC else PlayerGlyph.MIC_OFF, Color.White,
                        stringResource(if (state.microphone) R.string.call_mute else R.string.call_unmute))
                }
                Text(stringResource(if (state.microphone) R.string.call_mic_on else R.string.call_mic_off),
                    color = Color.White, style = MaterialTheme.typography.labelMedium)
            } else {
                val interaction = remember { MutableInteractionSource() }
                val held by interaction.collectIsPressedAsState()
                LaunchedEffect(held) { talk.microphone(held) }
                DisposableEffect(Unit) { onDispose { talk.microphone(false) } }
                TextButton(onClick = {}, interactionSource = interaction, modifier = Modifier.testTag("call-hold")) {
                    PlayerGlyph(PlayerGlyph.MIC, if (held) Color(0xFF60A5FA) else Color.White)
                    Spacer(Modifier.width(6.dp)); Text(stringResource(R.string.call_hold), color = Color.White)
                }
            }
            CallPhase.FAILED -> Text(stringResource(callReason(state.reason)), modifier = Modifier.widthIn(max = 210.dp),
                color = Color.White, style = MaterialTheme.typography.labelMedium)
            else -> Unit
        }
        if (state.phase == CallPhase.ACTIVE && capability.detectedDuplex != "push_to_talk") {
            Box {
                IconButton(onClick = { modeMenu = true }, modifier = Modifier.size(38.dp).testTag("call-mode")) {
                    Icon(Icons.Outlined.Settings, stringResource(R.string.call_mode), tint = Color.White, modifier = Modifier.size(19.dp))
                }
                DropdownMenu(expanded = modeMenu, onDismissRequest = { modeMenu = false }) {
                    listOf("auto" to R.string.call_mode_auto, "full" to R.string.call_mode_full,
                        "push_to_talk" to R.string.call_mode_hold).forEach { (key, label) ->
                        DropdownMenuItem(text = { Text(stringResource(label)) },
                            onClick = { modeMenu = false; talk.chooseMode(key) })
                    }
                }
            }
        }
        IconButton(onClick = talk::end, modifier = Modifier.testTag("call-hangup")) {
            PlayerGlyph(PlayerGlyph.HANGUP, Color(0xFFFDA4AF), stringResource(R.string.call_end))
        }
    }
}

private fun callReason(reason: String): Int = when (reason) {
    "camera_busy", "server_busy", "discovery_busy" -> R.string.call_busy
    "talk_credentials_required", "talk_authentication_failed", "talk_access_denied" -> R.string.call_bad_password
    "credential_save_failed" -> R.string.call_password_save_failed
    "camera_authentication_failed" -> R.string.call_camera_auth
    "backchannel_unsupported", "codec_unsupported", "transport_unsupported" -> R.string.call_unsupported
    "microphone_failed" -> R.string.call_permission
    else -> R.string.call_failed
}
