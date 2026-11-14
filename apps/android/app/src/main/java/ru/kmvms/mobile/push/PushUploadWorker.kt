package ru.kmvms.mobile.push

import android.content.Context
import android.os.Build
import androidx.work.*
import kotlinx.coroutines.CancellationException
import org.json.JSONObject
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.*
import java.util.concurrent.TimeUnit

/** One-time registration retry; never a periodic event-polling job. */
class PushUploadWorker(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result {
        val id = inputData.getString("profile") ?: return Result.failure()
        val vault = ProfileVault(applicationContext)
        val profile = vault.profiles().find { it.id == id } ?: return Result.success()
        val binding = PushStore(applicationContext).binding(id) ?: return Result.success()
        val saved = vault.signIn(id) ?: return Result.failure()
        val api = NasApi(ServerAddress.parse(profile.address)).apply {
            useSession(saved.access); sessionUser(profile.username)
            allowSessionRenewal({ vault.signIn(id) }, { expected, value -> vault.renewAccess(id, expected, value) })
        }
        try {
            upload(api, binding)
            PushStore(applicationContext).uploaded(id, binding.revision)
            return Result.success()
        } catch (cancelled: CancellationException) { throw cancelled }
        catch (error: Exception) {
            val code = (error as? NasException)?.httpStatus
            val retry = (code == null || code == 429 || code >= 500) && runAttemptCount < 10
            PushStore(applicationContext).status(id, if (retry) PushStatus.UPLOADING else if (code == 404) PushStatus.SERVER_UNSUPPORTED else PushStatus.SERVER_REJECTED, binding.revision)
            android.util.Log.w("KMVMS-Push", "registration_upload_http_${code ?: 0}")
            return if (retry) Result.retry() else Result.failure()
        }
    }
    companion object {
        suspend fun upload(api: NasApi, binding: PushBinding) {
            api.json("api/mobile/push/devices/${binding.instance}", "PUT", JSONObject()
                .put("revision", binding.revision).put("label", "${Build.MANUFACTURER} ${Build.MODEL}".take(80))
                .put("enabled", binding.enabled).put("subscription", if (binding.enabled) binding.subscription ?: throw IllegalStateException("push_endpoint_missing") else JSONObject.NULL))
        }
        fun enqueue(context: Context, profileId: String) {
            val request = OneTimeWorkRequestBuilder<PushUploadWorker>().setInputData(workDataOf("profile" to profileId))
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build()
            WorkManager.getInstance(context).enqueueUniqueWork("push-upload:$profileId", ExistingWorkPolicy.REPLACE, request)
        }
    }
}
