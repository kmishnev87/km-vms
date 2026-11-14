package ru.kmvms.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.data.ProfileVault
import ru.kmvms.mobile.data.ServerProfile
import java.util.UUID
import java.util.concurrent.CountDownLatch

@RunWith(AndroidJUnit4::class)
class ProfileLogoutTest {
    @Test fun aBackgroundVaultCannotReviveSignInAfterLogout() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val foreground = ProfileVault(context)
        val background = ProfileVault(context)
        val selected = foreground.selectedId()
        val own = ServerProfile(UUID.randomUUID().toString(), "Owned logout test", "http://192.0.2.1", "test-only")
        try {
            repeat(20) {
                foreground.save(own, "synthetic-test-only", "old-test-access")
                val start = CountDownLatch(1)
                val renewal = Thread { start.await(); background.renewAccess(own.id, "old-test-access", "new-test-access") }
                val logout = Thread { start.await(); foreground.forgetSignIn(own.id) }
                renewal.start(); logout.start(); start.countDown(); renewal.join(); logout.join()
                assertNull(background.signIn(own.id))
            }
        } finally {
            foreground.remove(own.id)
            if (selected != null) foreground.select(selected)
        }
    }
}
