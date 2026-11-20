package ru.kmvms.mobile.push

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Semaphore

import org.json.JSONObject
import org.unifiedpush.android.connector.PushService
import org.unifiedpush.android.connector.FailedReason
import org.unifiedpush.android.connector.data.PushEndpoint
import org.unifiedpush.android.connector.data.PushMessage
import ru.kmvms.mobile.data.ProfileVault
import ru.kmvms.mobile.diagnostics.*

class MobilePushService : PushService() {
    // The connector keeps the bound service alive for delivery; this work is
    // deliberately shorter than that window and does not block its binder.
    private val previewScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val previewSlots = Semaphore(4)
    override fun onNewEndpoint(endpoint: PushEndpoint, instance: String) {
        val keys = endpoint.pubKeySet ?: return failure(instance)
        // A distributor's temporary fallback uses the same encrypted protocol;
        // accept it and replace the registration again when the primary returns.
        if (!endpoint.url.startsWith("https://") || endpoint.url.length > 2048) return failure(instance)
        val subscription = JSONObject().put("endpoint", endpoint.url)
            .put("keys", JSONObject().put("p256dh", keys.pubKey).put("auth", keys.auth))
        val binding = PushStore(this).endpoint(instance, subscription) ?: return
        PushUploadWorker.enqueue(this, binding.profileId)
    }
    override fun onMessage(message: PushMessage, instance: String) {
        AppDiagnostics.record(DiagnosticCode.PUSH_RECEIVED, DiagnosticArea.PUSH)
        if (!message.decrypted) { AppDiagnostics.record(DiagnosticCode.PUSH_NOT_DECRYPTED, DiagnosticArea.PUSH); return }
        val binding = PushStore(this).byInstance(instance)
        if (binding == null) { AppDiagnostics.record(DiagnosticCode.PUSH_NO_BINDING, DiagnosticArea.PUSH); return }
        fun log(code: DiagnosticCode) = AppDiagnostics.record(code, DiagnosticArea.PUSH, profile = binding.profileId)
        val vault = ProfileVault(this)
        val profile = vault.profiles().find { it.id == binding.profileId }
        if (profile == null) { log(DiagnosticCode.PUSH_NO_PROFILE); return }
        if (runCatching { vault.signIn(profile.id) }.getOrNull() == null) { log(DiagnosticCode.PUSH_NO_SESSION); return }
        val badge = ru.kmvms.mobile.widget.WidgetUnread.parse(message.content)
        if (badge != null) {
            if (binding.badgeEnabled) ru.kmvms.mobile.widget.receiveWidgetUnread(this, profile, badge)
            return
        }
        if (!binding.enabled) { log(DiagnosticCode.PUSH_DISABLED); return }
        val envelope = runCatching { PushEnvelope.parse(message.content) }.getOrNull()
        if (envelope == null) { log(DiagnosticCode.PUSH_PARSE_FAILED); return }
        AppDiagnostics.record(DiagnosticCode.PUSH_PARSED, DiagnosticArea.PUSH, profile.id, envelope.eventId, envelope.cameraId)
        val app = applicationContext
        if (!envelope.preview || !MobileNotifications.allowed(app) || !previewSlots.tryAcquire()) {
            runCatching { MobileNotifications.show(app, profile, envelope) }.onFailure { log(DiagnosticCode.PUSH_POST_FAILED) }
            return
        }
        previewScope.launch {
            try {
                val image = prepareEventPreview(app, profile, envelope)
                val current = PushStore(app).byInstance(instance)
                if (current?.profileId == profile.id && current.enabled &&
                    runCatching { ProfileVault(app).signIn(profile.id) }.getOrNull() != null) {
                    runCatching { MobileNotifications.show(app, profile, envelope, image) }
                        .onFailure { log(DiagnosticCode.PUSH_POST_FAILED) }
                }
            } finally { previewSlots.release() }
        }
    }
    override fun onRegistrationFailed(reason: FailedReason, instance: String) {
        val state = when (reason) {
            FailedReason.NETWORK -> PushStatus.PROVIDER_NETWORK
            FailedReason.ACTION_REQUIRED -> PushStatus.PROVIDER_ACTION
            else -> PushStatus.PROVIDER_FAILED
        }
        // Typed diagnostic only: never log the token, endpoint or packet.
        android.util.Log.w("KMVMS-Push", "registration_${reason.name}")
        PushStore(this).byInstance(instance)?.let { PushStore(this).status(it.profileId, state) }
    }
    override fun onUnregistered(instance: String) { failure(instance) }
    override fun onTempUnavailable(instance: String) {
        PushStore(this).byInstance(instance)?.let { PushStore(this).status(it.profileId, PushStatus.TEMPORARY) }
    }
    private fun failure(instance: String) {
        PushStore(this).byInstance(instance)?.let { PushStore(this).status(it.profileId, PushStatus.FAILED) }
    }
}
