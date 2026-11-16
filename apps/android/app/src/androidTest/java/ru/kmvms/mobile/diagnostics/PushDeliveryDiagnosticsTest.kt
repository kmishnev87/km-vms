package ru.kmvms.mobile.diagnostics

import android.Manifest
import android.app.NotificationManager
import android.content.Context
import android.content.ContextWrapper
import android.os.Build
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.unifiedpush.android.connector.data.PushMessage
import ru.kmvms.mobile.data.ProfileVault
import ru.kmvms.mobile.data.ServerProfile
import ru.kmvms.mobile.push.*
import java.time.Instant
import java.util.UUID

/** Real Android parser + decrypted-message callback + OS posting, not provider/Doze proof. */
@RunWith(AndroidJUnit4::class)
class PushDeliveryDiagnosticsTest {
    @Test fun realOffsetEnvelopeReachesAndroidAndDropsHaveTypedReasons() = runBlocking {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext
        if (Build.VERSION.SDK_INT >= 33) instrumentation.uiAutomation.grantRuntimePermission(context.packageName, Manifest.permission.POST_NOTIFICATIONS)
        val vault = ProfileVault(context)
        val selected = vault.selectedId()
        val profile = ServerProfile(name = "Owned push component test", address = "http://192.0.2.1", username = "test-only")
        val manager = context.getSystemService(NotificationManager::class.java)
        val history = context.getSharedPreferences("push_delivery_ids", Context.MODE_PRIVATE)
        val ids = mutableListOf<String>()
        try {
            vault.save(profile, "test-only-password", "test-only-session")
            val binding = PushStore(context).begin(profile.id, "test-only-vapid")
            val service = MobilePushService()
            ContextWrapper::class.java.getDeclaredMethod("attachBaseContext", Context::class.java).apply { isAccessible = true }.invoke(service, context)
            if (Build.VERSION.SDK_INT <= 31) assertTrue("old Android Instant parser reproduces the regression",
                runCatching { Instant.parse("2026-11-16T04:25:38+00:00") }.isFailure)
            fun packet(time: String): ByteArray = JSONObject().put("v", 1).put("type", "camera_event")
                .put("event_id", UUID.randomUUID().toString().also { ids.add(it) }).put("camera_id", 1).put("camera_name", "Gate")
                .put("kind", "intrusion").put("occurred_at", time).toString().toByteArray()
            for (time in listOf("2026-11-16T04:25:38+00:00", "2026-11-16T04:25:38.123456Z")) {
                service.onMessage(PushMessage(packet(time), true), binding.instance)
            }
            service.onMessage(PushMessage(packet("malformed"), true), binding.instance)
            service.onMessage(PushMessage(packet("2026-11-16T04:25:38Z"), false), binding.instance)
            service.onMessage(PushMessage(packet("2026-11-16T04:25:38Z"), true), "unknown-test-instance")
            val entries = AppDiagnostics.snapshot(context).getJSONArray("journal")
            val codes = (0 until entries.length()).map { entries.getJSONObject(it).getString("code") }
            assertTrue(codes.containsAll(listOf("PUSH_PARSED", "PUSH_POSTED", "PUSH_PARSE_FAILED", "PUSH_NOT_DECRYPTED", "PUSH_NO_BINDING")))
            for (id in ids.take(2)) assertTrue(manager.activeNotifications.any { it.tag == "${profile.id}:$id" })
            for (id in ids.drop(2)) assertFalse(manager.activeNotifications.any { it.tag == "${profile.id}:$id" })
            assertFalse(entries.toString().contains("test-only-password"))
            assertFalse(entries.toString().contains("malformed"))
        } finally {
            MobileNotifications.clear(context, profile.id)
            vault.remove(profile.id)
            if (selected != null) vault.select(selected)
            val prior = JSONArray(history.getString("delivered_v1", "[]"))
            val retained = (0 until prior.length()).map { prior.getString(it) }.filterNot { it.startsWith(profile.id + ":") }
            history.edit().putString("delivered_v1", JSONArray(retained).toString()).remove("last_received.${profile.id}").commit()
        }
    }
}
