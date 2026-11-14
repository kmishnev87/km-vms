package ru.kmvms.mobile.push

import org.json.JSONObject
import org.unifiedpush.android.connector.PushService
import org.unifiedpush.android.connector.FailedReason
import org.unifiedpush.android.connector.data.PushEndpoint
import org.unifiedpush.android.connector.data.PushMessage
import ru.kmvms.mobile.data.ProfileVault

class MobilePushService : PushService() {
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
        if (!message.decrypted) return
        val binding = PushStore(this).byInstance(instance)?.takeIf { it.enabled } ?: return
        val vault = ProfileVault(this)
        val profile = vault.profiles().find { it.id == binding.profileId } ?: return
        if (runCatching { vault.signIn(profile.id) }.getOrNull() == null) return
        val envelope = runCatching { PushEnvelope.parse(message.content) }.getOrNull() ?: return
        MobileNotifications.show(this, profile, envelope)
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
