package ru.kmvms.mobile.push

import android.content.Context
import android.graphics.Bitmap
import android.os.SystemClock
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.data.NasException
import ru.kmvms.mobile.data.ProfileVault
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.ServerProfile
import ru.kmvms.mobile.diagnostics.AppDiagnostics
import ru.kmvms.mobile.diagnostics.DiagnosticArea
import ru.kmvms.mobile.diagnostics.DiagnosticCode
import ru.kmvms.mobile.events.EventFrameCache

// UnifiedPush 3.3.5 binds for at most five seconds. Leave time to post the
// single notification; fast responses still complete without a delay.
private const val PREVIEW_WINDOW_MS = 4100L

/** Drain work queued by older APKs without updating a notification after it was posted. */
class EventPreviewWorker(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result = Result.success()
}

internal suspend fun prepareEventPreview(context: Context, profile: ServerProfile, event: PushEnvelope): Bitmap? {
    val started = SystemClock.elapsedRealtime()
    val vault = ProfileVault(context)
    val bitmap = try {
        withTimeoutOrNull(PREVIEW_WINDOW_MS) {
            val saved = vault.signIn(profile.id) ?: return@withTimeoutOrNull null
            val api = NasApi(ServerAddress.parse(profile.address)).apply {
                useSession(saved.access)
                sessionUser(profile.username)
                allowSessionRenewal({ vault.signIn(profile.id) }, { expected, value -> vault.renewAccess(profile.id, expected, value) })
            }
            val retry = listOf(240L, 400L, 600L, 400L)
            var image: Bitmap? = null
            for (attempt in 0..retry.size) {
                try {
                    image = EventFrameCache.load(context, profile.id, event.eventId) { api.eventSnapshot(event.eventId, quick = true) }
                    break
                } catch (error: NasException) {
                    // An ONVIF image can still be arriving. Never retry permissions or a dead connection.
                    if (error.httpStatus != 404 || attempt == retry.size) break
                    delay(retry[attempt])
                }
            }
            image
        }
    } catch (cancelled: CancellationException) { throw cancelled }
    catch (_: Exception) { null }
    AppDiagnostics.record(if (bitmap != null) DiagnosticCode.PUSH_PREVIEW_READY else DiagnosticCode.PUSH_PREVIEW_MISSING,
        DiagnosticArea.PUSH, profile.id, event.eventId, event.cameraId, (SystemClock.elapsedRealtime() - started).toInt())
    return bitmap
}
