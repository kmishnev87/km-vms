package ru.kmvms.mobile

import android.Manifest
import android.app.Notification
import android.app.NotificationManager
import android.content.Context
import android.graphics.Bitmap
import android.os.Build
import android.os.SystemClock
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.data.ServerProfile
import ru.kmvms.mobile.events.CameraEventKind
import ru.kmvms.mobile.push.MobileNotifications
import ru.kmvms.mobile.push.PushEnvelope
import java.time.Instant
import java.util.UUID

/** OS notification component test, NOT a claim of provider/Doze delivery. */
@RunWith(AndroidJUnit4::class)
class MobileNotificationTest {
    @Test fun systemNotificationHasTwoImmutableActionsAndDeduplicates() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext
        if (Build.VERSION.SDK_INT >= 33) instrumentation.uiAutomation.grantRuntimePermission(context.packageName, Manifest.permission.POST_NOTIFICATIONS)
        val profile = ServerProfile(UUID.randomUUID().toString(), "Owned notification component test", "http://192.0.2.1", "test-only")
        val event = PushEnvelope(UUID.randomUUID().toString(), 1, "Gate", CameraEventKind.INTRUSION, Instant.now(), objects = setOf(CameraEventKind.PERSON))
        val manager = context.getSystemService(NotificationManager::class.java)
        val history = context.getSharedPreferences("push_delivery_ids", Context.MODE_PRIVATE)
        val preview = Bitmap.createBitmap(64, 36, Bitmap.Config.ARGB_8888).apply { eraseColor(0xff356080.toInt()) }
        try {
            MobileNotifications.show(context, profile, event, preview)
            val deadline = SystemClock.elapsedRealtime() + 3000
            var found = manager.activeNotifications.firstOrNull { it.tag == "${profile.id}:${event.eventId}" }
            while (found == null && SystemClock.elapsedRealtime() < deadline) {
                SystemClock.sleep(50)
                found = manager.activeNotifications.firstOrNull { it.tag == "${profile.id}:${event.eventId}" }
            }
            assertNotNull(found)
            val posted = found!!
            assertEquals(Notification.VISIBILITY_PRIVATE, posted.notification.visibility)
            assertEquals(2, posted.notification.actions.size)
            assertTrue(posted.notification.extras.getString(Notification.EXTRA_TEMPLATE).orEmpty().endsWith("BigPictureStyle"))
            assertTrue(posted.notification.flags and Notification.FLAG_ONLY_ALERT_ONCE != 0)
            if (Build.VERSION.SDK_INT >= 31) posted.notification.actions.forEach { assertTrue(it.actionIntent.isImmutable) }
            assertEquals(context.packageName, posted.notification.contentIntent.creatorPackage)
            MobileNotifications.show(context, profile, event)
            SystemClock.sleep(100)
            assertEquals(posted.postTime, manager.activeNotifications.first { it.tag == posted.tag }.postTime)
        } finally {
            preview.recycle()
            MobileNotifications.clear(context, profile.id)
            val prior = JSONArray(history.getString("delivered_v1", "[]"))
            val retained = (0 until prior.length()).map { prior.getString(it) }.filterNot { it.startsWith(profile.id + ":") }
            history.edit().putString("delivered_v1", JSONArray(retained).toString()).remove("last_received.${profile.id}").commit()
        }
        // NotificationManager.cancel is asynchronous across the system-service boundary.
        val cancelDeadline = SystemClock.elapsedRealtime() + 2_000
        while (manager.activeNotifications.any { it.tag?.startsWith(profile.id + ":") == true } && SystemClock.elapsedRealtime() < cancelDeadline)
            SystemClock.sleep(25)
        assertFalse(manager.activeNotifications.any { it.tag?.startsWith(profile.id + ":") == true })
    }
}
