package ru.kmvms.mobile.widget

import android.appwidget.AppWidgetManager
import android.content.Intent
import android.net.Uri
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.data.*
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class AlertWidgetStorageTest {
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext
    @Test fun pinReceiptIsOneUseAndCannotBindAForeignOrMissingWidget() {
        val store = AlertWidgetStore(context)
        val binding = AlertWidgetBinding("widget-test-only", "fixture")
        val now = System.currentTimeMillis()
        val nonce = store.pending(binding, now)
        val callback = Intent(context, AlertWidgetPinnedReceiver::class.java)
            .setData(Uri.parse("kmvmswidget://pin/$nonce"))
            .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID)
        AlertWidgetPinnedReceiver().onReceive(context, callback)
        assertEquals(binding, store.consume(nonce, now + 1))
        assertNull(store.consume(nonce, now + 2))
        val expired = store.pending(binding, now)
        assertNull(store.consume(expired, now + 86_400_001))
        val ownId = Int.MAX_VALUE - 10
        assertNull(store.binding(ownId))
        try { store.bind(ownId, binding); assertEquals(binding, store.binding(ownId)) }
        finally { store.remove(ownId) }
        assertNull(store.binding(ownId))
    }
    @Test fun clientForBoundNasNeverSelectsAnotherConnectionAndDoesNotCopyCredentialsIntoWidgetStorage() {
        val vault = ProfileVault(context)
        val oldSelected = vault.selectedId()
        val one = ServerProfile(UUID.randomUUID().toString(), "Widget fixture A", "http://192.0.2.1:8088", "fixture-a")
        val two = ServerProfile(UUID.randomUUID().toString(), "Widget fixture B", "http://192.0.2.2:8088", "fixture-b")
        try {
            vault.save(one, "test-only", "fixture-token")
            vault.save(two, "test-only", "fixture-token")
            val client = alertWidgetClient(context, AlertWidgetBinding(one.id, one.username))
            assertEquals(one.address + "/", client.address.endpoint("").toString())
            assertEquals(two.id, vault.selectedId())
        } finally {
            vault.remove(one.id); vault.remove(two.id)
            context.getSharedPreferences("nas_profiles", 0).edit().let { edit ->
                if (oldSelected == null) edit.remove("selected") else edit.putString("selected", oldSelected)
            }.apply()
        }
    }
}
