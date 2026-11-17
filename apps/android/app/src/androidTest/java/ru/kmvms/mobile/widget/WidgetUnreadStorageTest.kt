package ru.kmvms.mobile.widget

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.data.*
import ru.kmvms.mobile.push.*
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class WidgetUnreadStorageTest {
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext
    @Test fun accountAddressAndRevisionFencesPreventAStaleOrOtherNasCount() {
        val store = WidgetUnreadStore(context)
        val profile = ServerProfile(UUID.randomUUID().toString(), "Counter fixture", "http://192.0.2.1", "one")
        try {
            val value = WidgetUnread(UUID.randomUUID().toString(), 8, 20)
            assertTrue(store.install(profile, value))
            assertNull(store.get(profile.copy(username = "two")))
            assertNull(store.get(profile.copy(address = "https://192.0.2.2")))
            assertFalse(store.receive(profile, value.copy(revision = 7, count = 30)))
            assertFalse(store.receive(profile, value.copy(epoch = UUID.randomUUID().toString(), revision = 100, count = 40)))
            assertTrue(store.receive(profile, value.copy(revision = 9, count = 0)))
            assertFalse(store.install(profile, value)) // A delayed HTTP response cannot undo a newer push.
            assertEquals(0, store.get(profile)?.count)
            store.clear(profile.id)
            assertNull(store.get(profile))
        } finally { store.clear(profile.id) }
    }
    @Test fun silentTransportNeverEnablesPhoneAlertsAndSurvivesTheirExplicitDisable() {
        val vault = ProfileVault(context)
        val previous = vault.selectedId()
        val profile = ServerProfile(UUID.randomUUID().toString(), "Counter fixture", "http://192.0.2.1", "test-only")
        try {
            vault.save(profile, "synthetic-only", "synthetic-access")
            val store = PushStore(context)
            assertTrue(store.shouldOfferSetup(profile.id))
            val counter = store.counter(profile.id, true, "test-only")!!
            assertFalse(counter.enabled)
            assertTrue(counter.badgeEnabled && counter.transportActive)
            assertTrue(store.shouldOfferSetup(profile.id))
            val endpoint = store.endpoint(counter.instance, JSONObject().put("synthetic", true))!!
            assertFalse(endpoint.enabled)
            store.begin(profile.id, "test-only")
            val disabled = store.disable(profile.id)!!
            assertFalse(disabled.enabled)
            assertTrue(disabled.badgeEnabled && disabled.transportActive)
            assertFalse(store.shouldOfferSetup(profile.id))
            val off = store.disable(profile.id, all = true)!!
            assertFalse(off.transportActive)
            assertNull(store.endpoint(off.instance, JSONObject()))
            assertNull(store.counter(profile.id, true, "test-only", expectedRevision = counter.revision))
            assertFalse(store.binding(profile.id)!!.transportActive)
        } finally { vault.remove(profile.id); if (previous != null) vault.select(previous) }
    }
}
