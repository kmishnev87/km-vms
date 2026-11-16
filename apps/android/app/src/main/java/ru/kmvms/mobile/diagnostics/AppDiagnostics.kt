package ru.kmvms.mobile.diagnostics

import android.app.ActivityManager
import android.app.Application
import android.app.NotificationManager
import android.content.Context
import android.os.Build
import android.os.PowerManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import ru.kmvms.mobile.BuildConfig
import ru.kmvms.mobile.data.ProfileVault
import ru.kmvms.mobile.push.MobileNotifications
import ru.kmvms.mobile.push.PushStore
import org.unifiedpush.android.connector.UnifiedPush
import java.io.File
import java.util.Locale
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit

class MobileApplication : Application() {
    override fun onCreate() { super.onCreate(); AppDiagnostics.start(this) }
}

object AppDiagnostics {
    private val executor = ThreadPoolExecutor(1, 1, 5, TimeUnit.SECONDS, ArrayBlockingQueue(256),
        { task -> Thread(task, "kmvms-diagnostics").apply { isDaemon = true } }, ThreadPoolExecutor.AbortPolicy())
        .apply { allowCoreThreadTimeOut(true) }
    @Volatile private var journal: DiagnosticJournal? = null
    fun start(context: Context) {
        synchronized(this) {
            if (journal != null) return
            journal = DiagnosticJournal(File(context.noBackupFilesDir, "apk-diagnostics"))
            record(DiagnosticCode.APP_STARTED)
        }
    }
    fun record(code: DiagnosticCode, area: DiagnosticArea = DiagnosticArea.APP, profile: String? = null,
        event: String? = null, camera: Long? = null, number: Int? = null) {
        val target = journal ?: return
        val entry = DiagnosticEntry(System.currentTimeMillis(), code, area, profile?.let(DiagnosticEntry::profileKey), event, camera, number)
        // Diagnostics must not break playback/push if its bounded queue is full.
        runCatching { executor.execute { runCatching { target.append(entry) } } }
    }
    suspend fun snapshot(context: Context): JSONObject = withContext(Dispatchers.IO) {
        start(context)
        // FIFO barrier includes prior callbacks without blocking the main thread.
        // AbortPolicy rejects a full queue instead of dropping a waiting Future.
        val records = executor.submit<List<JSONObject>> { journal!!.snapshot() }.get(10, TimeUnit.SECONDS)
        val manager = context.getSystemService(NotificationManager::class.java)
        val store = PushStore(context)
        val history = context.getSharedPreferences("push_delivery_ids", Context.MODE_PRIVATE)
        val bindings = ProfileVault(context).profiles().take(16).mapNotNull { profile -> store.binding(profile.id)?.let { binding ->
            JSONObject().put("profile", DiagnosticEntry.profileKey(profile.id)).put("enabled", binding.enabled)
                .put("status", binding.status.name).put("uploaded_at", binding.uploadedAt)
                .put("last_received_at", history.getLong("last_received.${profile.id}", 0))
        } }
        val exits = if (Build.VERSION.SDK_INT >= 30) runCatching {
            context.getSystemService(ActivityManager::class.java).getHistoricalProcessExitReasons(context.packageName, 0, 5)
                .map { JSONObject().put("time", it.timestamp).put("reason", it.reason).put("status", it.status) }
        }.getOrDefault(emptyList()) else emptyList()
        JSONObject().put("sdk", Build.VERSION.SDK_INT).put("os", Build.VERSION.RELEASE.take(32))
            .put("manufacturer", Build.MANUFACTURER.take(80)).put("model", Build.MODEL.take(80))
            .put("language", Locale.getDefault().toLanguageTag().take(40)).put("version_code", BuildConfig.VERSION_CODE)
            .put("push_provider", runCatching { UnifiedPush.getSavedDistributor(context) }.getOrNull()
                ?.takeIf { it.matches(Regex("[A-Za-z0-9_.]{1,160}")) }.orEmpty())
            .put("notifications_allowed", MobileNotifications.allowed(context))
            .put("channel_enabled", manager.getNotificationChannel(MobileNotifications.CHANNEL)?.importance != NotificationManager.IMPORTANCE_NONE)
            .put("battery_unrestricted", context.getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(context.packageName))
            .put("bindings", JSONArray(bindings)).put("exits", JSONArray(exits)).put("journal", JSONArray(records))
    }
}
