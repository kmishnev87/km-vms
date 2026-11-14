package ru.kmvms.mobile.ui.camera

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import org.json.JSONObject
import ru.kmvms.mobile.R
import ru.kmvms.mobile.camera.EncoderDraft
import ru.kmvms.mobile.ui.Muted

@Composable fun CameraEncodingDialog(contract:JSONObject,dismiss:()->Unit,apply:(JSONObject)->Unit) {
    var draft by remember(contract){mutableStateOf(EncoderDraft.from(contract))}
    var confirm by remember{mutableStateOf(false)}
    val labels=mapOf("codec" to R.string.encoder_codec,"codec_profile" to R.string.encoder_profile,
        "resolution" to R.string.encoder_resolution,"fps" to R.string.encoder_fps,"bitrate_mode" to R.string.encoder_bitrate_mode,
        "bitrate" to R.string.encoder_bitrate,"iframe_interval" to R.string.encoder_iframe,"quality" to R.string.encoder_quality)
    AlertDialog(onDismissRequest=dismiss,title={Text(stringResource(R.string.camera_encoding))},text={
        Column(Modifier.fillMaxWidth().heightIn(max=430.dp).verticalScroll(rememberScrollState()),verticalArrangement=Arrangement.spacedBy(12.dp)){
            EncoderDraft.names.chunked(2).forEach{pair->Row(horizontalArrangement=Arrangement.spacedBy(8.dp)){
                pair.forEach{key->Box(Modifier.weight(1f)){
                    val meta=draft.meta(key);val options=draft.options(key);val label=stringResource(labels.getValue(key))
                    if(options.isNotEmpty()&&meta.optBoolean("writable"))ChoiceField(label,draft[key],options,true,"encoder-$key"){draft=draft.change(key,it)}
                    else OutlinedTextField(draft[key],{draft=draft.change(key,it)},label={Text(label)},enabled=meta.optBoolean("writable"),singleLine=true,
                        keyboardOptions=KeyboardOptions(keyboardType=if(key in setOf("fps","bitrate","iframe_interval","quality"))KeyboardType.Decimal else KeyboardType.Text),modifier=Modifier.fillMaxWidth())
                }}
            }}
            if(contract.optJSONArray("shared_profile_tokens")?.length()?.let{it>1}==true)Text(stringResource(R.string.encoder_shared),color=Muted,style=MaterialTheme.typography.bodySmall)
        }
    },confirmButton={TextButton(onClick={confirm=true},enabled=draft.valid()&&draft.diff().length()>0){Text(stringResource(R.string.apply_to_camera))}},
        dismissButton={TextButton(onClick=dismiss){Text(stringResource(R.string.close))}})
    if(confirm)AlertDialog(onDismissRequest={confirm=false},text={Text(stringResource(R.string.encoder_change_confirmation))},
        confirmButton={TextButton(onClick={confirm=false;apply(draft.diff());dismiss()}){Text(stringResource(R.string.apply))}},
        dismissButton={TextButton(onClick={confirm=false}){Text(stringResource(R.string.cancel))}})
}
