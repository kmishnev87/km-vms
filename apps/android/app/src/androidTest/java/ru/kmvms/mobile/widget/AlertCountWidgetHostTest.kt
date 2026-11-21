package ru.kmvms.mobile.widget

import android.appwidget.AppWidgetHost
import android.appwidget.AppWidgetManager
import android.content.Intent
import android.net.Uri
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.core.EventAction
import ru.kmvms.mobile.data.*
import ru.kmvms.mobile.push.NotificationNavigation
import java.util.UUID
import org.unifiedpush.android.connector.data.PushMessage
import ru.kmvms.mobile.push.MobilePushService
import ru.kmvms.mobile.push.PushStore

/** Isolated host IDs; runner grants BIND_APPWIDGET only to the debug package. */
@RunWith(AndroidJUnit4::class)
class AlertCountWidgetHostTest {
    @Test fun onlyTwoByOneCanBeAddedAndOpensLiveAndHistoryForItsBoundNas() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val manager = AppWidgetManager.getInstance(context)
        val host = AppWidgetHost(context, 117202611)
        val vault = ProfileVault(context)
        val previous = vault.selectedId()
        val one = ServerProfile(UUID.randomUUID().toString(), "Owned widget A", "http://192.0.2.1", "test-only")
        val two = ServerProfile(UUID.randomUUID().toString(), "Owned widget B", "http://192.0.2.2", "test-only")
        val store = AlertWidgetStore(context)
        val ids = mutableListOf<Int>()
        try {
            vault.save(one, "synthetic-only", "synthetic-access")
            vault.save(two, "synthetic-only", "synthetic-access")
            val installed = manager.installedProviders.filter { it.provider.packageName == context.packageName }
            assertEquals(listOf(alertWidgetComponent(context)), installed.map { it.provider })
            run {
                val id = host.allocateAppWidgetId().also(ids::add)
                assertTrue("debug host needs adb appwidget grantbind", manager.bindAppWidgetIdIfAllowed(id, alertWidgetComponent(context)))
                store.bind(id, AlertWidgetBinding(one.id, one.username))
                assertTrue(ownedAlertWidget(context, id))
                val info = manager.getAppWidgetInfo(id)
                assertEquals(2, info.targetCellWidth)
                assertEquals(1, info.targetCellHeight)
                for ((action, address, expected) in listOf(
                    Triple(AlertWidgetProvider.OPEN_LIVE, "live", EventAction.LIVE),
                    Triple(AlertWidgetProvider.OPEN_EVENTS, "events", EventAction.OPEN),
                )) {
                    val intent = Intent(action, Uri.parse("kmvmswidget://$address/$id"))
                        .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id)
                    assertTrue(AlertWidgetProvider.openDestination(context, intent))
                        assertEquals(one.id, NotificationNavigation.pending.value?.profileId)
                        assertEquals(expected, NotificationNavigation.pending.value?.action)
                        assertNull(NotificationNavigation.pending.value?.eventId)
                        assertEquals(two.id, vault.selectedId())
                        NotificationNavigation.pending.value?.let { NotificationNavigation.consume(it.key) }
                }
                AlertWidgetProvider.render(context, id)
            }
            assertTrue(hasCountWidgets(context, one.id))
            assertFalse(hasCountWidgets(context, two.id))
            val value = WidgetUnread(UUID.randomUUID().toString(), 1, 10)
            WidgetUnreadStore(context).install(one, value)
            assertTrue(receiveWidgetUnread(context, one, value.copy(revision = 2, count = 0)))
            assertEquals(0, WidgetUnreadStore(context).get(one)?.count)
            assertFalse(receiveWidgetUnread(context, one, value.copy(revision = 1, count = 10)))
            assertFalse(receiveWidgetUnread(context, two, value.copy(revision = 3, count = 12)))
            val route = PushStore(context).counter(one.id, true, "test-only")!!
            val notifications = context.getSystemService(android.app.NotificationManager::class.java)
            val before = notifications.activeNotifications.map { it.key }.toSet()
            val service = MobilePushService()
            android.content.ContextWrapper::class.java.getDeclaredMethod("attachBaseContext", android.content.Context::class.java)
                .apply { isAccessible = true }.invoke(service, context)
            service.onMessage(PushMessage(value.copy(revision = 3, count = 7).json().toString().toByteArray(), true), route.instance)
            assertEquals(7, WidgetUnreadStore(context).get(one)?.count)
            assertEquals(before, notifications.activeNotifications.map { it.key }.toSet())
            service.onMessage(PushMessage(value.copy(revision = 4, count = 9).json().toString().toByteArray(), false), route.instance)
            assertEquals(7, WidgetUnreadStore(context).get(one)?.count)
            PushStore(context).disable(one.id, all = true)
            service.onMessage(PushMessage(value.copy(revision = 5, count = 99).json().toString().toByteArray(), true), route.instance)
            assertEquals(7, WidgetUnreadStore(context).get(one)?.count)
            assertFalse(AlertWidgetProvider.openDestination(context, Intent().setAction(AlertWidgetProvider.OPEN_EVENTS)
                .setData(Uri.parse("kmvmswidget://events/999999"))
                .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, 999999)))
        } finally {
            ids.forEach(store::remove)
            host.deleteHost()
            WidgetUnreadStore(context).clear(one.id)
            vault.remove(one.id); vault.remove(two.id)
            if (previous != null) vault.select(previous)
            NotificationNavigation.pending.value?.let { NotificationNavigation.consume(it.key) }
        }
    }
}
