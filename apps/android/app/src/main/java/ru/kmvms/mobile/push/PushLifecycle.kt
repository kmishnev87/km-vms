package ru.kmvms.mobile.push

import android.content.Context
import kotlinx.coroutines.CancellationException
import org.json.JSONObject
import org.unifiedpush.android.connector.UnifiedPush
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.data.ProfileVault

object PushLifecycle {
    fun signOut(context: Context, profileId: String): PushBinding? {
        val disabled = PushStore(context).disable(profileId, all = true)
        if (disabled != null) UnifiedPush.unregister(context, disabled.instance)
        MobileNotifications.clear(context, profileId)
        ru.kmvms.mobile.events.EventFrameCache.clear(context, profileId)
        ru.kmvms.mobile.widget.WidgetUnreadStore(context).clear(profileId)
        ru.kmvms.mobile.widget.AlertWidgetProvider.refresh(context)
        return disabled
    }
    /** Registration renewal only when the person opens the application. */
    fun opened(context: Context) {
        val store = PushStore(context)
        val vault = ProfileVault(context)
        vault.profiles().forEach { profile ->
            val binding = store.binding(profile.id) ?: return@forEach
            if (!binding.transportActive || binding.subscription == null) return@forEach
            if (binding.uploadedAt > 0 && System.currentTimeMillis() - binding.uploadedAt > 30L * 86400000) {
                store.renew(profile.id)
                PushUploadWorker.enqueue(context, profile.id)
            } else if (binding.status == PushStatus.UPLOADING) PushUploadWorker.enqueue(context, profile.id)
        }
    }

    /** One status check for the authenticated foreground profile; nothing while asleep. */
    suspend fun reconcile(context: Context, profileId: String, api: NasApi, current: () -> Boolean) {
        val store = PushStore(context)
        val binding = store.binding(profileId)?.takeIf {
            it.transportActive && it.subscription != null && it.status != PushStatus.UPLOADING && it.status != PushStatus.REGISTERING
        } ?: return
        val remote = try {
            api.json("api/mobile/push/devices/${binding.instance}") as JSONObject
        } catch (cancelled: CancellationException) { throw cancelled }
        catch (_: Exception) { return }
        if (!current()) return
        when (PushRecovery.forServer(binding.revision, remote.optInt("revision", -1),
            remote.optBoolean("enabled", true), remote.optBoolean("badge_enabled"), remote.optString("status"))) {
            PushRecovery.REUPLOAD -> if (store.renew(profileId, expectedRevision = binding.revision) != null) {
                PushUploadWorker.enqueue(context, profileId)
            }
            PushRecovery.NEEDS_SERVICE -> store.status(profileId, PushStatus.SERVER_REJECTED, binding.revision)
            PushRecovery.NONE -> Unit
        }
    }
}
