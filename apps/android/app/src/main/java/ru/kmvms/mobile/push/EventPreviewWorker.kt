package ru.kmvms.mobile.push

import android.content.Context
import android.graphics.BitmapFactory
import androidx.work.*
import kotlinx.coroutines.CancellationException
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.data.ProfileVault
import java.util.UUID
import java.util.concurrent.TimeUnit

/** One optional download for an active notification. No periodic work/profile switch. */
class EventPreviewWorker(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result {
        val profileId = inputData.getString("profile") ?: return Result.success()
        val id = inputData.getString("event") ?: return Result.success()
        val queued = inputData.getLong("queued", 0)
        if (runCatching { UUID.fromString(id).toString() == id }.getOrDefault(false).not()
            || System.currentTimeMillis() - queued !in 0L..90_000L) return Result.success()
        val context = applicationContext
        val tag = "$profileId:$id"
        if (!MobileNotifications.active(context, tag)) return Result.success()
        val vault = ProfileVault(context)
        try {
            val profile = vault.profiles().firstOrNull { it.id == profileId } ?: return Result.success()
            val saved = vault.signIn(profileId) ?: return Result.success()
            if (PushStore(context).binding(profileId)?.enabled != true) return Result.success()
            val api = NasApi(ServerAddress.parse(profile.address)).apply {
                useSession(saved.access)
                sessionUser(profile.username)
                allowSessionRenewal({ vault.signIn(profileId) }, { expected, value -> vault.renewAccess(profileId, expected, value) })
            }
            val jpeg = api.eventSnapshot(id)
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size, bounds)
            if (bounds.outWidth in 1..800 && bounds.outHeight in 1..600 && vault.signIn(profileId) != null
                && PushStore(context).binding(profileId)?.enabled == true) {
                BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size)?.let { MobileNotifications.addPreview(context, tag, it) }
            }
        } catch (cancelled: CancellationException) { throw cancelled }
        catch (_: Exception) { /* No preview is a valid result; keep the text and both actions. */ }
        return Result.success()
    }

    companion object {
        internal fun enqueue(context: Context, profileId: String, event: PushEnvelope) {
            val request = OneTimeWorkRequestBuilder<EventPreviewWorker>()
                .setInputData(workDataOf("profile" to profileId, "event" to event.eventId, "queued" to System.currentTimeMillis()))
                .setInitialDelay(if (event.aiCondition == null) 7L else 0L, TimeUnit.SECONDS)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork("push-preview:$profileId:${event.eventId}", ExistingWorkPolicy.KEEP, request)
        }
    }
}
