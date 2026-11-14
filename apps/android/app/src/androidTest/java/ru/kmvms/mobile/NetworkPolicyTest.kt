package ru.kmvms.mobile

import android.security.NetworkSecurityPolicy
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi

@RunWith(AndroidJUnit4::class)
class NetworkPolicyTest {
    @Test fun httpNasSupportDoesNotDependOnPrivateBuildHosts() {
        val policy = NetworkSecurityPolicy.getInstance()
        listOf("192.168.31.41", "192.168.13.41", "198.51.100.42", "nas.example").forEach { host ->
            assertTrue("Explicit HTTP NAS address must be supported: $host", policy.isCleartextTrafficPermitted(host))
        }
    }

    @Test fun acceptingHttpDoesNotEnableCrossOriginOrHttpsDowngradeRedirects() {
        val client = NasApi(ServerAddress.parse("https://nas.example/")).mediaClient
        assertFalse(client.followRedirects)
        assertFalse(client.followSslRedirects)
    }
}
