package ru.kmvms.mobile.push

import android.content.Context
import org.unifiedpush.android.connector.UnifiedPush
import ru.kmvms.mobile.data.ProfileVault

object PushLifecycle {
    fun signOut(context: Context, profileId: String): PushBinding? {
        val disabled = PushStore(context).disable(profileId, all = true)
        if (disabled != null) UnifiedPush.unregister(context, disabled.instance)
        MobileNotifications.clear(context, profileId)
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
}
