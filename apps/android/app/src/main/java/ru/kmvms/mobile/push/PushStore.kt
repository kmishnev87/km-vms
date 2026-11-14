package ru.kmvms.mobile.push

import android.content.Context
import org.json.JSONObject
import ru.kmvms.mobile.data.ProfileVault
import java.util.UUID

enum class PushStatus { OFF, REGISTERING, UPLOADING, READY, NO_PROVIDER, SERVER_UNSUPPORTED, FAILED, TEMPORARY, PROVIDER_FAILED, PROVIDER_NETWORK, PROVIDER_ACTION, SERVER_REJECTED }
data class PushBinding(val profileId: String, val instance: String, val enabled: Boolean, val vapid: String,
    val revision: Int, val subscription: JSONObject?, val status: PushStatus, val uploadedAt: Long = 0) {
    fun json() = JSONObject().put("instance", instance).put("enabled", enabled).put("vapid", vapid)
        .put("revision", revision).put("subscription", subscription ?: JSONObject.NULL).put("status", status.name).put("uploadedAt", uploadedAt)
    override fun toString() = "PushBinding(status=$status, subscription=[redacted])"
}

/** Serializes callbacks, logout and upload completion across all store instances. */
class PushStore(context: Context) {
    private val vault = ProfileVault(context.applicationContext)
    fun binding(profileId: String): PushBinding? = synchronized(lock) {
        vault.pushState(profileId)?.let { value -> runCatching {
            PushBinding(profileId, value.getString("instance"), value.getBoolean("enabled"), value.getString("vapid"),
                value.getInt("revision"), value.optJSONObject("subscription"), PushStatus.valueOf(value.getString("status")), value.optLong("uploadedAt"))
        }.getOrNull() }
    }
    fun byInstance(instance: String): PushBinding? = synchronized(lock) {
        vault.profiles().firstNotNullOfOrNull { profile -> binding(profile.id)?.takeIf { it.instance == instance } }
    }
    fun begin(profileId: String, vapid: String): PushBinding = synchronized(lock) {
        val old = binding(profileId)
        val value = old?.copy(enabled = true, vapid = vapid, status = PushStatus.REGISTERING)
            ?: PushBinding(profileId, UUID.randomUUID().toString(), true, vapid, 1, null, PushStatus.REGISTERING)
        save(value); value
    }
    fun endpoint(instance: String, subscription: JSONObject): PushBinding? = synchronized(lock) {
        val old = byInstance(instance)?.takeIf { it.enabled } ?: return@synchronized null
        val value = old.copy(subscription = subscription, revision = old.revision + 1, status = PushStatus.UPLOADING)
        save(value); value
    }
    fun disable(profileId: String): PushBinding? = synchronized(lock) {
        val old = binding(profileId) ?: return@synchronized null
        val value = old.copy(enabled = false, revision = old.revision + 1, status = PushStatus.OFF)
        save(value); value
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
    fun renew(profileId: String): PushBinding? = synchronized(lock) {
        val old = binding(profileId)?.takeIf { it.enabled && it.subscription != null } ?: return@synchronized null
        val value = old.copy(revision = old.revision + 1, status = PushStatus.UPLOADING)
        save(value); value
    }
    private fun save(binding: PushBinding) { vault.putPushState(binding.profileId, binding.json()) }
    companion object { private val lock = Any() }
}
