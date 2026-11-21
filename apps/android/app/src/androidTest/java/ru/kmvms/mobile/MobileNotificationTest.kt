package ru.kmvms.mobile

import android.Manifest
import android.app.Notification
import android.app.NotificationManager
import android.content.Context
import android.graphics.Bitmap
import android.graphics.drawable.BitmapDrawable
import android.os.Build
import android.os.SystemClock
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.TextView
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.Buffer
import org.json.JSONArray
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.data.ServerProfile
import ru.kmvms.mobile.data.ProfileVault
import ru.kmvms.mobile.events.AiEventCondition
import ru.kmvms.mobile.events.EventFrameCache
import ru.kmvms.mobile.events.CameraEventKind
import ru.kmvms.mobile.push.MobileNotifications
import ru.kmvms.mobile.push.PushEnvelope
import ru.kmvms.mobile.push.prepareEventPreview
import java.io.ByteArrayOutputStream
import java.time.Instant
import java.util.UUID
import java.util.concurrent.TimeUnit

/** OS notification component test, NOT a claim of provider/Doze delivery. */
@RunWith(AndroidJUnit4::class)
class MobileNotificationTest {
    @Test fun savedAiScenePhotoSurvivesAColdSlowNasResponse() = runBlocking {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext
        if (Build.VERSION.SDK_INT >= 33) instrumentation.uiAutomation.grantRuntimePermission(context.packageName, Manifest.permission.POST_NOTIFICATIONS)
        val vault = ProfileVault(context)
        MockWebServer().use { server ->
            val profile = ServerProfile(UUID.randomUUID().toString(), "Preview fixture", server.url("/").toString(), "fixture")
            val event = PushEnvelope(UUID.randomUUID().toString(), 7, "PTZ", CameraEventKind.MOTION, Instant.now(),
                aiCondition = AiEventCondition.SCENE, preview = true)
            val jpeg = ByteArrayOutputStream().apply {
                val bitmap = Bitmap.createBitmap(160, 90, Bitmap.Config.ARGB_8888)
                bitmap.eraseColor(0xff464f58.toInt())
                bitmap.compress(Bitmap.CompressFormat.JPEG, 90, this)
                bitmap.recycle()
            }.toByteArray()
            vault.save(profile, "fixture-pass", "fixture-session")
            server.enqueue(MockResponse().setHeader("Content-Type", "image/jpeg").setHeadersDelay(1700, TimeUnit.MILLISECONDS)
                .setBody(Buffer().write(jpeg)))
            try {
                val image = prepareEventPreview(context, profile, event)
                assertNotNull("A saved JPEG should survive a 1.7s NAS response", image)
                assertEquals(160, image!!.width)
                assertEquals("/api/camera-events/" + event.eventId + "/snapshot", server.takeRequest(1, TimeUnit.SECONDS)?.path)
                MobileNotifications.show(context, profile, event, image, manufacturer = "generic")
                val manager = context.getSystemService(NotificationManager::class.java)
                val deadline = SystemClock.elapsedRealtime() + 2000
                var posted = manager.activeNotifications.firstOrNull { it.tag == profile.id + ":" + event.eventId }
                while (posted == null && SystemClock.elapsedRealtime() < deadline) {
                    SystemClock.sleep(25)
                    posted = manager.activeNotifications.firstOrNull { it.tag == profile.id + ":" + event.eventId }
                }
                assertNotNull(posted)
                assertTrue(posted!!.notification.extras.getString(Notification.EXTRA_TEMPLATE).orEmpty().endsWith("BigPictureStyle"))
                assertEquals(2, posted.notification.actions.size)
                assertEquals(1, server.requestCount)
            } finally {
                MobileNotifications.clear(context, profile.id)
                EventFrameCache.clear(context, profile.id)
                vault.remove(profile.id)
            }
        }
    }

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
            MobileNotifications.show(context, profile, event, preview, manufacturer = "generic")
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

    @Suppress("DEPRECATION") // The expanded RemoteViews is part of the actual notification handed to Android.
    @Test fun huaweiUsesExpandedPhotoAndKeepsPrivateGroupedActions() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext
        if (Build.VERSION.SDK_INT >= 33) instrumentation.uiAutomation.grantRuntimePermission(context.packageName, Manifest.permission.POST_NOTIFICATIONS)
        val localized = AppLocales.wrap(context)
        val profile = ServerProfile(UUID.randomUUID().toString(), "NAS fixture", "http://192.0.2.1", "fixture")
        val event = PushEnvelope(UUID.randomUUID().toString(), 1, "Gate", CameraEventKind.PERSON, Instant.now())
        val textOnly = event.copy(eventId = UUID.randomUUID().toString())
        val manager = context.getSystemService(NotificationManager::class.java)
        val history = context.getSharedPreferences("push_delivery_ids", Context.MODE_PRIVATE)
        val frameColor = 0xff356080.toInt()
        val preview = Bitmap.createBitmap(800, 600, Bitmap.Config.ARGB_8888).apply { eraseColor(frameColor) }
        fun posted(id: String): Notification {
            val deadline = SystemClock.elapsedRealtime() + 3_000
            do {
                val notification = manager.activeNotifications.firstOrNull { it.tag == "${profile.id}:$id" }
                if (notification != null) return notification.notification
                SystemClock.sleep(25)
            } while (SystemClock.elapsedRealtime() < deadline)
            throw AssertionError("Expected one posted notification")
        }

        try {
            MobileNotifications.show(context, profile, event, preview, manufacturer = "HUAWEI")
            val card = posted(event.eventId)
            assertTrue(card.extras.getString(Notification.EXTRA_TEMPLATE).orEmpty().endsWith("DecoratedCustomViewStyle"))
            assertEquals(Notification.VISIBILITY_PRIVATE, card.visibility)
            assertEquals("nas:${profile.id}", card.group)
            assertEquals(MobileNotifications.CHANNEL, card.channelId)
            assertNull("A tiny thumbnail is not used", card.getLargeIcon())
            assertTrue(card.flags and Notification.FLAG_ONLY_ALERT_ONCE != 0)
            assertEquals(listOf(localized.getString(R.string.push_watch_live), localized.getString(R.string.push_watch_event)),
                card.actions.map { it.title.toString() })
            assertEquals(context.packageName, card.contentIntent.creatorPackage)
            if (Build.VERSION.SDK_INT >= 31) card.actions.forEach { assertTrue(it.actionIntent.isImmutable) }
            val expanded = requireNotNull(card.bigContentView)
            instrumentation.runOnMainSync {
                val view = expanded.apply(localized, FrameLayout(localized))
                assertEquals("NAS fixture · Gate", view.findViewById<TextView>(R.id.notification_event_title).text.toString())
                assertTrue(view.findViewById<TextView>(R.id.notification_event_text).text.isNotBlank())
                val frame = view.findViewById<ImageView>(R.id.notification_event_frame)
                assertEquals(localized.getString(R.string.event_photo), frame.contentDescription.toString())
                val decoded = (frame.drawable as BitmapDrawable).bitmap
                assertEquals(frameColor, decoded.getPixel(decoded.width / 2, decoded.height / 2))
            }
            MobileNotifications.show(context, profile, textOnly, manufacturer = "Huawei")
            val withoutPhoto = posted(textOnly.eventId)
            assertTrue(withoutPhoto.extras.getString(Notification.EXTRA_TEMPLATE).orEmpty().endsWith("BigTextStyle"))
            assertNull(withoutPhoto.bigContentView)
            assertEquals(2, withoutPhoto.actions.size)
        } finally {
            MobileNotifications.clear(context, profile.id)
            preview.recycle()
            val prior = JSONArray(history.getString("delivered_v1", "[]"))
            val retained = (0 until prior.length()).map { prior.getString(it) }.filterNot { it.startsWith(profile.id + ":") }
            history.edit().putString("delivered_v1", JSONArray(retained).toString()).remove("last_received.${profile.id}").commit()
        }
    }
}
