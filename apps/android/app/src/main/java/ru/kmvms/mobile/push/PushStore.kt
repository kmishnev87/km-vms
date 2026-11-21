package ru.kmvms.mobile.push

import android.content.Context
import org.json.JSONObject
import ru.kmvms.mobile.data.ProfileVault
import java.util.UUID
import ru.kmvms.mobile.diagnostics.*

enum class PushStatus { OFF, REGISTERING, UPLOADING, READY, NO_PROVIDER, SERVER_UNSUPPORTED, FAILED, TEMPORARY, PROVIDER_FAILED, PROVIDER_NETWORK, PROVIDER_ACTION, SERVER_REJECTED }
data class PushBinding(val profileId: String, val instance: String, val enabled: Boolean, val vapid: String,
    val revision: Int, val subscription: JSONObject?, val status: PushStatus, val uploadedAt: Long = 0,
    val badgeEnabled: Boolean = false, val phoneConfigured: Boolean = true) {
    val transportActive get() = enabled || badgeEnabled
    fun json() = JSONObject().put("instance", instance).put("enabled", enabled).put("vapid", vapid)
        .put("revision", revision).put("subscription", subscription ?: JSONObject.NULL).put("status", status.name).put("uploadedAt", uploadedAt)
        .put("badgeEnabled", badgeEnabled)
        .put("phoneConfigured", phoneConfigured)
    override fun toString() = "PushBinding(status=$status, subscription=[redacted])"
}

/** Serializes callbacks, logout and upload completion across all store instances. */
class PushStore(context: Context) {
    private val vault = ProfileVault(context.applicationContext)
    // A legacy binding is already a choice, including an explicitly disabled one.
    fun shouldOfferSetup(profileId: String): Boolean = synchronized(lock) {
        !vault.pushSetupAnswered(profileId) && binding(profileId)?.phoneConfigured != true
    }
    fun answerSetup(profileId: String) = synchronized(lock) { vault.answerPushSetup(profileId) }
    fun binding(profileId: String): PushBinding? = synchronized(lock) {
        vault.pushState(profileId)?.let { value -> runCatching {
            PushBinding(profileId, value.getString("instance"), value.getBoolean("enabled"), value.getString("vapid"),
                value.getInt("revision"), value.optJSONObject("subscription"), PushStatus.valueOf(value.getString("status")), value.optLong("uploadedAt"), value.optBoolean("badgeEnabled"), value.optBoolean("phoneConfigured", true))
        }.getOrNull() }
    }
    fun byInstance(instance: String): PushBinding? = synchronized(lock) {
        vault.profiles().firstNotNullOfOrNull { profile -> binding(profile.id)?.takeIf { it.instance == instance } }
    }
    fun begin(profileId: String, vapid: String): PushBinding = synchronized(lock) {
        val old = binding(profileId)
        val value = old?.copy(enabled = true, vapid = vapid, revision = old.revision + 1, status = PushStatus.REGISTERING, phoneConfigured = true)
            ?: PushBinding(profileId, UUID.randomUUID().toString(), true, vapid, 1, null, PushStatus.REGISTERING)
        save(value); value
    }
    fun endpoint(instance: String, subscription: JSONObject): PushBinding? = synchronized(lock) {
        val old = byInstance(instance)?.takeIf { it.transportActive } ?: return@synchronized null
        val value = old.copy(subscription = subscription, revision = old.revision + 1, status = PushStatus.UPLOADING)
        save(value); value
    }
    fun disable(profileId: String, all: Boolean = false): PushBinding? = synchronized(lock) {
        val old = binding(profileId) ?: return@synchronized null
        val value = old.copy(enabled = false, badgeEnabled = if (all) false else old.badgeEnabled, phoneConfigured = true,
            revision = old.revision + 1, status = PushStatus.OFF)
        save(value); value
    }
    /** Silent widget updates never change the person's phone-notification choice. */
    fun counter(profileId: String, enabled: Boolean, vapid: String, expectedRevision: Int? = null): PushBinding? = synchronized(lock) {
        val old = binding(profileId)
        if (expectedRevision != null && (old?.revision ?: 0) != expectedRevision) return@synchronized null
        if (old == null && !enabled) return@synchronized null
        if (old != null && old.badgeEnabled == enabled && old.vapid == vapid) return@synchronized old
        val next = old?.copy(badgeEnabled = enabled, vapid = vapid, revision = old.revision + 1,
            subscription = old.subscription.takeIf { old.vapid == vapid })
            ?: PushBinding(profileId, UUID.randomUUID().toString(), false, vapid, 1, null, PushStatus.OFF, badgeEnabled = enabled, phoneConfigured = false)
        save(next); next
    }
    fun uploaded(profileId: String, revision: Int): Boolean = synchronized(lock) {
        val old = binding(profileId)?.takeIf { it.revision == revision } ?: return@synchronized false
        save(old.copy(status = if (old.enabled) PushStatus.READY else PushStatus.OFF, uploadedAt = System.currentTimeMillis())); true
    }
    fun status(profileId: String, status: PushStatus, expectedRevision: Int? = null) = synchronized(lock) {
        binding(profileId)?.takeIf { expectedRevision == null || it.revision == expectedRevision }?.let {
            save(it.copy(status = if (it.enabled) status else PushStatus.OFF))
        }
    }
    fun renew(profileId: String, expectedRevision: Int? = null): PushBinding? = synchronized(lock) {
        val old = binding(profileId)?.takeIf { it.transportActive && it.subscription != null &&
            (expectedRevision == null || it.revision == expectedRevision) } ?: return@synchronized null
        val value = old.copy(revision = old.revision + 1, status = PushStatus.UPLOADING)
        save(value); value
    }
    private fun save(binding: PushBinding) {
        vault.putPushState(binding.profileId, binding.json())
        AppDiagnostics.record(DiagnosticCode.PUSH_STATE, DiagnosticArea.PUSH, binding.profileId, number = binding.status.ordinal)
    }
    companion object { private val lock = Any() }
}
