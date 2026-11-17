package ru.kmvms.mobile.widget

import android.app.Activity
import android.content.Context
import androidx.work.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.suspendCancellableCoroutine
import org.json.JSONObject
import org.unifiedpush.android.connector.UnifiedPush
import ru.kmvms.mobile.data.ProfileVault
import ru.kmvms.mobile.data.ServerProfile
import ru.kmvms.mobile.push.PushStore
import ru.kmvms.mobile.push.PushUploadWorker
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume

/** One-shot synchronization at binding/app opening, NEVER a periodic poll. */
class WidgetUnreadSync(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result {
        val id = inputData.getString("profile") ?: return Result.failure()
        val vault = ProfileVault(applicationContext)
        val profile = vault.profiles().find { it.id == id } ?: return Result.success()
        val store = PushStore(applicationContext)
        if (vault.signIn(id) == null) {
            WidgetUnreadStore(applicationContext).clear(id)
            AlertWidgetProvider.refresh(applicationContext)
            return Result.success()
        }
        try {
            val initialRevision = store.binding(id)?.revision ?: 0
            val wanted = hasCountWidgets(applicationContext, id)
            val api = alertWidgetClient(applicationContext, AlertWidgetBinding(id, profile.username))
            if (!wanted) {
                val old = store.binding(id) ?: return Result.success()
                if (!old.badgeEnabled) return Result.success()
                val next = store.counter(id, false, old.vapid) ?: return Result.success()
                if (!next.transportActive) UnifiedPush.unregister(applicationContext, next.instance)
                PushUploadWorker.upload(api, next)
                store.uploaded(id, next.revision)
                return Result.success()
            }
            val config = api.json("api/mobile/push/config?badge=true") as JSONObject
            if (!config.optBoolean("widget_badge_v1")) return Result.failure()
            val vapid = config.getString("vapid_public_key").also { require(it.matches(Regex("[A-Za-z0-9_-]{87}"))) }
            WidgetUnreadStore(applicationContext).install(profile, WidgetUnread.parse(config.getJSONObject("unread_badge")))
            // Recheck after the network request: deletion/sign-out must win.
            if (!hasCountWidgets(applicationContext, id) || vault.signIn(id) == null ||
                vault.profiles().none { it.id == id && it.address == profile.address && it.username == profile.username }) return Result.success()
            val old = store.binding(id)
            val next = store.counter(id, true, vapid, expectedRevision = initialRevision) ?: return Result.success()
            if (next.subscription == null) UnifiedPush.register(applicationContext, next.instance, vapid = vapid)
            else if (old?.revision != next.revision) {
                PushUploadWorker.upload(api, next)
                store.uploaded(id, next.revision)
            }
            AlertWidgetProvider.refresh(applicationContext)
            return Result.success()
        } catch (cancelled: CancellationException) { throw cancelled }
        catch (_: Exception) {
            // Only failed real work gets bounded backoff; nothing runs when idle.
            return if (runAttemptCount < 4) Result.retry() else Result.failure()
        }
    }
    companion object {
        fun enqueue(context: Context, profileId: String) {
            val request = OneTimeWorkRequestBuilder<WidgetUnreadSync>().setInputData(workDataOf("profile" to profileId))
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build()
            WorkManager.getInstance(context).enqueueUniqueWork("widget-unread:$profileId", ExistingWorkPolicy.REPLACE, request)
        }
        fun opened(context: Context) {
            ProfileVault(context).profiles().filter { hasCountWidgets(context, it.id) || PushStore(context).binding(it.id)?.badgeEnabled == true }
                .forEach { enqueue(context, it.id) }
            AlertWidgetProvider.refresh(context)
        }
        /** Verify support and a push distributor BEFORE asking the launcher to add the counter. */
        internal suspend fun prepare(activity: Activity, profile: ServerProfile): Boolean {
            val config = alertWidgetClient(activity, AlertWidgetBinding(profile.id, profile.username))
                .json("api/mobile/push/config?badge=true") as JSONObject
            if (!config.optBoolean("widget_badge_v1")) return false
            val snapshot = WidgetUnread.parse(config.getJSONObject("unread_badge"))
            val available = suspendCancellableCoroutine { continuation ->
                UnifiedPush.tryUseCurrentOrDefaultDistributor(activity) { ready ->
                    if (continuation.isActive) continuation.resume(ready)
                }
            }
            if (available) WidgetUnreadStore(activity).install(profile, snapshot)
            return available
        }
    }
}
