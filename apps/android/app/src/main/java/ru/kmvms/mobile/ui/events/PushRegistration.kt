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
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
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

/** The first-login offer and Settings use exactly the same registration flow. */
internal data class PushRegistrationUi(
    val binding: PushBinding?, val permission: Boolean, val busy: Boolean, val status: PushStatus,
    val enable: () -> Unit, val disable: () -> Unit, val chooseService: () -> Unit, val test: () -> Unit,
    val testFeedback: Int? = null,
)

@Composable internal fun rememberPushRegistration(api: NasApi, profileId: String, onMessage: (String) -> Unit): PushRegistrationUi {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val store = remember { PushStore(context) }
    var binding by remember(profileId) { mutableStateOf(store.binding(profileId)) }
    var busy by remember(profileId) { mutableStateOf(false) }
    var permission by remember(profileId) { mutableStateOf(MobileNotifications.allowed(context)) }
    var failure by remember(profileId) { mutableStateOf<PushStatus?>(null) }
    var testFeedback by remember(profileId) { mutableStateOf<Int?>(null) }
    var awaitingSettings by rememberSaveable(profileId) { mutableStateOf(false) }
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    val testAccepted = stringResource(R.string.push_test_accepted)
    val failed = stringResource(R.string.push_failed)
    DisposableEffect(profileId, lifecycle) {
        val preferences = context.getSharedPreferences("nas_profiles", Context.MODE_PRIVATE)
        val listener = android.content.SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
            if (key == "push.$profileId") binding = store.binding(profileId)
        }
        val observer = LifecycleEventObserver { _, event -> if (event == Lifecycle.Event.ON_RESUME) {
            permission = MobileNotifications.allowed(context)
            binding = store.binding(profileId)
        } }
        preferences.registerOnSharedPreferenceChangeListener(listener)
        lifecycle.addObserver(observer)
        onDispose { preferences.unregisterOnSharedPreferenceChangeListener(listener); lifecycle.removeObserver(observer) }
    }
    fun begin(pickService: Boolean = false) {
        if (busy) return
        busy = true
        failure = null
        testFeedback = null
        scope.launch {
            try {
                val config = api.json("api/mobile/push/config") as JSONObject
                require(config.getInt("protocol") == 1)
                val vapid = config.getString("vapid_public_key").also { require(it.matches(Regex("[A-Za-z0-9_-]{87}"))) }
                val next = store.begin(profileId, vapid)
                binding = next
                val activity = context.activity() ?: error("activity_missing")
                val registered: (Boolean) -> Unit = { available ->
                    // A late service-picker callback must not revive an explicit disable.
                    if (store.binding(profileId)?.let { it.enabled && it.instance == next.instance } == true) {
                        if (available) UnifiedPush.register(context, next.instance, vapid = vapid)
                        else store.status(profileId, PushStatus.NO_PROVIDER)
                    }
                }
                if (pickService) UnifiedPush.tryPickDistributor(activity, registered)
                else UnifiedPush.tryUseCurrentOrDefaultDistributor(activity, registered)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                val unsupported = (error as? NasException)?.httpStatus == 404
                failure = if (unsupported) PushStatus.SERVER_UNSUPPORTED else PushStatus.FAILED
                store.status(profileId, failure!!)
                onMessage(if (unsupported) context.getString(R.string.push_server_update) else failed)
            } finally { busy = false }
        }
    }
    val permissionLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        permission = MobileNotifications.allowed(context)
        if (granted && permission) begin()
    }
    fun enable() {
        store.answerSetup(profileId)
        permission = MobileNotifications.allowed(context)
        if (permission) begin()
        else if (Build.VERSION.SDK_INT >= 33) permissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        else {
            awaitingSettings = true
            context.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName))
        }
    }
    LaunchedEffect(permission, awaitingSettings) {
        if (permission && awaitingSettings) { awaitingSettings = false; begin() }
    }
    val status = failure ?: if (busy && binding?.enabled != true) PushStatus.REGISTERING else binding?.status ?: PushStatus.OFF
    return PushRegistrationUi(binding, permission, busy, status,
        enable = ::enable,
        disable = {
            store.answerSetup(profileId)
            failure = null
            testFeedback = null
            store.disable(profileId)?.let { UnifiedPush.unregister(context, it.instance); PushUploadWorker.enqueue(context, profileId) }
            MobileNotifications.clear(context, profileId)
        },
        chooseService = { store.answerSetup(profileId); if (permission) begin(true) else enable() },
        test = { if (!busy) {
            busy = true
            testFeedback = null
            scope.launch {
                try {
                    val current = store.binding(profileId) ?: return@launch
                    api.json("api/mobile/push/devices/${current.instance}/test", "POST", JSONObject())
                    testFeedback = R.string.push_test_accepted
                    onMessage(testAccepted)
                } catch (cancelled: CancellationException) { throw cancelled }
                catch (_: Exception) { testFeedback = R.string.push_failed; onMessage(failed) }
                finally { busy = false }
            }
        } }, testFeedback = testFeedback)
}

internal fun pushStatusText(permission: Boolean, status: PushStatus): Int = if (!permission) R.string.push_permission else when (status) {
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

private tailrec fun Context.activity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.activity()
    else -> null
}
