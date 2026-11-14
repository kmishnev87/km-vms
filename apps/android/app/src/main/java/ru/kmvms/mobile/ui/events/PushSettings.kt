package ru.kmvms.mobile.ui.events

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import org.json.JSONObject
import org.unifiedpush.android.connector.UnifiedPush
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.data.NasException
import ru.kmvms.mobile.push.*
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.server.Panel

@Composable fun PushSettings(api: NasApi, profileId: String, onMessage: (String) -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val store = remember { PushStore(context) }
    var binding by remember(profileId) { mutableStateOf(store.binding(profileId)) }
    var busy by remember(profileId) { mutableStateOf(false) }
    var permission by remember { mutableStateOf(MobileNotifications.allowed(context)) }
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    val testAccepted = stringResource(R.string.push_test_accepted)
    val failed = stringResource(R.string.push_failed)
    DisposableEffect(profileId, lifecycle) {
        val preferences = context.getSharedPreferences("nas_profiles", Context.MODE_PRIVATE)
        val listener = android.content.SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
            if (key == "push.$profileId") binding = store.binding(profileId)
        }
        val observer = LifecycleEventObserver { _, event -> if (event == Lifecycle.Event.ON_RESUME) {
            permission = MobileNotifications.allowed(context); binding = store.binding(profileId)
        } }
        preferences.registerOnSharedPreferenceChangeListener(listener)
        lifecycle.addObserver(observer)
        onDispose { preferences.unregisterOnSharedPreferenceChangeListener(listener); lifecycle.removeObserver(observer) }
    }
    fun begin(pickService: Boolean = false) {
        scope.launch {
            busy = true
            try {
                val config = api.json("api/mobile/push/config") as JSONObject
                require(config.getInt("protocol") == 1)
                val vapid = config.getString("vapid_public_key").also { require(it.matches(Regex("[A-Za-z0-9_-]{87}"))) }
                val next = store.begin(profileId, vapid)
                val activity = context.activity() ?: error("activity_missing")
                val registered: (Boolean) -> Unit = { available ->
                    if (available) UnifiedPush.register(context, next.instance, vapid = vapid)
                    else store.status(profileId, PushStatus.NO_PROVIDER)
                }
                if (pickService) UnifiedPush.tryPickDistributor(activity, registered)
                else UnifiedPush.tryUseCurrentOrDefaultDistributor(activity, registered)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                store.status(profileId, if ((error as? NasException)?.httpStatus == 404) PushStatus.SERVER_UNSUPPORTED else PushStatus.FAILED)
                onMessage(if ((error as? NasException)?.httpStatus == 404) context.getString(R.string.push_server_update) else failed)
            } finally { busy = false }
        }
    }
    val permissionLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        permission = MobileNotifications.allowed(context)
        if (granted && permission) begin()
    }
    Panel {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(stringResource(R.string.push_phone), style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
            Switch(binding?.enabled == true, { enabled ->
                if (!enabled) {
                    store.disable(profileId)?.let { UnifiedPush.unregister(context, it.instance); PushUploadWorker.enqueue(context, profileId) }
                    MobileNotifications.clear(context, profileId)
                } else if (MobileNotifications.allowed(context)) begin()
                else if (Build.VERSION.SDK_INT >= 33) permissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                else context.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName))
            }, enabled = !busy, modifier = Modifier.testTag("push-enable"))
        }
        val text = if (!permission) R.string.push_permission else when (binding?.status ?: PushStatus.OFF) {
            PushStatus.OFF -> R.string.push_off
            PushStatus.REGISTERING -> R.string.push_registering
            PushStatus.UPLOADING -> R.string.push_uploading
            PushStatus.READY -> R.string.push_ready
            PushStatus.NO_PROVIDER -> R.string.push_no_provider
            PushStatus.SERVER_UNSUPPORTED -> R.string.push_server_update
            PushStatus.FAILED -> R.string.push_failed
            PushStatus.TEMPORARY -> R.string.push_temporary
            PushStatus.PROVIDER_FAILED -> R.string.push_provider_failed
            PushStatus.PROVIDER_NETWORK -> R.string.push_provider_network
            PushStatus.PROVIDER_ACTION -> R.string.push_provider_action
            PushStatus.SERVER_REJECTED -> R.string.push_server_rejected
        }
        Text(stringResource(text), style = MaterialTheme.typography.bodySmall, color = Muted, modifier = Modifier.testTag("push-status"))
        Text(stringResource(R.string.push_scope), style = MaterialTheme.typography.bodySmall, color = Muted)
        TextButton(onClick = { if (permission) begin(true) }, enabled = permission && !busy,
            modifier = Modifier.testTag("push-provider")) { Text(stringResource(R.string.push_choose_service)) }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (binding?.enabled == true) OutlinedButton(onClick = { begin() }, enabled = !busy,
                modifier = Modifier.weight(1f).testTag("push-retry")) { Text(stringResource(R.string.retry)) }
            OutlinedButton(onClick = { scope.launch {
                busy = true
                try {
                    val current = store.binding(profileId) ?: return@launch
                    api.json("api/mobile/push/devices/${current.instance}/test", "POST", JSONObject())
                    onMessage(testAccepted)
                } catch (cancelled: CancellationException) { throw cancelled }
                catch (_: Exception) { onMessage(failed) }
                finally { busy = false }
            } }, enabled = binding?.status == PushStatus.READY && permission && !busy,
                modifier = Modifier.weight(1f).testTag("push-test")) { Text(stringResource(R.string.push_test)) }
        }
    }
}

private tailrec fun Context.activity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.activity()
    else -> null
}
