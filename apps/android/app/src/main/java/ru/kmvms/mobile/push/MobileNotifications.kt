package ru.kmvms.mobile.push

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import ru.kmvms.mobile.AppLocales
import ru.kmvms.mobile.MainActivity
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.ServerProfile
import ru.kmvms.mobile.ui.events.eventLabels
import ru.kmvms.mobile.events.CameraEventKind
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import org.json.JSONArray
import ru.kmvms.mobile.diagnostics.*

object MobileNotifications {
    const val CHANNEL = "camera-events-v1"
    private val lock = Any()
    fun allowed(context: Context) = (Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) && NotificationManagerCompat.from(context).areNotificationsEnabled()
    fun show(original: Context, profile: ServerProfile, event: PushEnvelope) = synchronized(lock) {
        fun log(code: DiagnosticCode) = AppDiagnostics.record(code, DiagnosticArea.PUSH, profile.id, event.eventId, event.cameraId)
        if (!allowed(original)) { log(DiagnosticCode.PUSH_PERMISSION_BLOCKED); return@synchronized }
        val context = AppLocales.wrap(original)
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(CHANNEL, context.getString(R.string.push_channel), NotificationManager.IMPORTANCE_HIGH))
        if (manager.getNotificationChannel(CHANNEL)?.importance == NotificationManager.IMPORTANCE_NONE) {
            log(DiagnosticCode.PUSH_CHANNEL_BLOCKED); return@synchronized
        }
        val history = context.getSharedPreferences("push_delivery_ids", Context.MODE_PRIVATE)
        val key = "${profile.id}:${event.eventId}"
        val previous = JSONArray(history.getString("delivered_v1", "[]"))
        val delivered = (0 until previous.length()).map { previous.getString(it) }
        if (key in delivered) { log(DiagnosticCode.PUSH_DUPLICATE); return@synchronized }
        val text = if (event.test) context.getString(R.string.push_test_received) else eventLabels(event.kind, event.objects, event.aiCondition).joinToString(" · ") { context.getString(it) } + " · " +
            DateTimeFormatter.ofPattern("HH:mm:ss").withZone(ZoneId.systemDefault()).format(event.occurred)
        val builder = NotificationCompat.Builder(context, CHANNEL).setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(if (event.test) profile.name else profile.name + " · " + event.cameraName)
            .setContentText(text).setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setCategory(NotificationCompat.CATEGORY_EVENT).setPriority(NotificationCompat.PRIORITY_HIGH)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE).setAutoCancel(true).setOnlyAlertOnce(true)
            .setGroup("nas:${profile.id}")
        if (!event.test) {
            builder.setContentIntent(open(context, profile.id, event.eventId, "open"))
            if (event.cameraId != null) builder
                .addAction(0, context.getString(R.string.push_watch_live), open(context, profile.id, event.eventId, "live"))
                .addAction(0, context.getString(R.string.push_watch_event), open(context, profile.id, event.eventId, "archive"))
        }
        val critical = event.kind in setOf(CameraEventKind.FIRE, CameraEventKind.TAMPER)
        if (event.test || critical || !localLiveAttention.watching(profile.id, event.cameraId)) {
            if (runCatching { manager.notify(key, 1, builder.build()) }.isFailure) {
                log(DiagnosticCode.PUSH_POST_FAILED); return@synchronized
            }
            // Means handed to Android, not proof that the person saw a banner.
            log(DiagnosticCode.PUSH_POSTED)
        } else log(DiagnosticCode.PUSH_LIVE_SUPPRESSED)
        history.edit().putString("delivered_v1", JSONArray(delivered.takeLast(255) + key).toString()).apply()
        history.edit().putLong("last_received.${profile.id}", System.currentTimeMillis()).apply()
    }
    private fun open(context: Context, profile: String, event: String, action: String): PendingIntent {
        val intent = Intent(context, MainActivity::class.java).setData(Uri.parse("kmvms://notification/$profile/$event/$action"))
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        return PendingIntent.getActivity(context, 0, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }
    fun clear(context: Context, profile: String) {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.activeNotifications.filter { it.tag?.startsWith("$profile:") == true }.forEach { manager.cancel(it.tag, it.id) }
    }
}
