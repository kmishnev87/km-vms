package ru.kmvms.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.data.ProfileVault
import ru.kmvms.mobile.data.ServerProfile
import ru.kmvms.mobile.push.PushStore
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class PushSetupChoiceTest {
    @Test fun newAndPreexistingProfilesWithoutAChoiceAreOfferedOnceAndIndependently() {
        withProfiles { vault, store, first, second ->
            assertTrue(store.shouldOfferSetup(first.id))
            assertTrue(store.shouldOfferSetup(second.id))
            store.answerSetup(first.id)
            assertFalse(PushStore(InstrumentationRegistry.getInstrumentation().targetContext).shouldOfferSetup(first.id))
            assertTrue(store.shouldOfferSetup(second.id))
            assertNull(store.binding(first.id)) // Declining never enables delivery or creates a fake registration.
            vault.forgetSignIn(first.id)
            vault.save(first, "synthetic-only", "synthetic-access")
            assertFalse(store.shouldOfferSetup(first.id)) // Signing in again does not reset a refusal.
        }
    }

    @Test fun legacyEnabledAndExplicitlyDisabledBindingsNeverGetReenabledOrPrompted() {
        withProfiles { _, store, first, second ->
            store.begin(first.id, "test-only")
            store.begin(second.id, "test-only")
            store.disable(second.id)
            assertFalse(store.shouldOfferSetup(first.id))
            assertFalse(store.shouldOfferSetup(second.id))
            assertEquals(true, store.binding(first.id)?.enabled)
            assertEquals(false, store.binding(second.id)?.enabled)
        }
    }

    @Test fun removingAProfileClearsItsChoiceWithoutChangingOtherProfiles() {
        withProfiles { vault, store, first, second ->
            store.answerSetup(first.id)
            store.answerSetup(second.id)
            vault.remove(first.id)
            vault.save(first, "synthetic-only", "synthetic-access")
            assertTrue(store.shouldOfferSetup(first.id))
            assertFalse(store.shouldOfferSetup(second.id))
        }
    }

    private fun withProfiles(test: (ProfileVault, PushStore, ServerProfile, ServerProfile) -> Unit) {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val vault = ProfileVault(context)
        val selected = vault.selectedId()
        val first = ServerProfile(UUID.randomUUID().toString(), "Owned setup test A", "http://192.0.2.1", "test-only")
        val second = ServerProfile(UUID.randomUUID().toString(), "Owned setup test B", "http://192.0.2.2", "test-only")
        try {
            vault.save(first, "synthetic-only", "synthetic-access")
            vault.save(second, "synthetic-only", "synthetic-access")
            test(vault, PushStore(context), first, second)
        } finally {
            vault.remove(first.id); vault.remove(second.id)
            if (selected != null) vault.select(selected)
        }
    }
}
