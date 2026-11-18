package ru.kmvms.mobile.data

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.core.ServerAddress

class TalkCredentialReasonTest {
    @Test fun onlyKnownBoundedTalkReasonsAreExposedToThePasswordDialog() = runBlocking {
        MockWebServer().use { server ->
            server.start()
            val api = NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("synthetic-access") }
            val cases = listOf(
                Triple("api/talk/cameras/7/credentials", """{"detail":"talk_authentication_failed"}""", "talk_authentication_failed"),
                Triple("api/talk/cameras/7/credentials", """{"detail":"talk_access_denied"}""", "talk_access_denied"),
                Triple("api/talk/sessions", """{"detail":"camera_busy"}""", "camera_busy"),
                Triple("api/talk/cameras/7/credentials", """{"detail":"untrusted-sensitive-body"}""", null),
                Triple("api/settings", """{"detail":"talk_authentication_failed"}""", null),
                Triple("api/talk/cameras/7/credentials", "{\"detail\":\"" + "x".repeat(9000) + "\"}", null),
            )
            for ((path, body, expected) in cases) {
                server.enqueue(MockResponse().setResponseCode(409).setBody(body))
                try {
                    api.json(path, "PUT", JSONObject().put("password", "synthetic-password"))
                    fail("Expected rejection")
                } catch (error: NasException) {
                    assertEquals(expected, error.publicReason)
                    assertFalse(error.toString().contains("sensitive"))
                    assertFalse(error.toString().contains("synthetic-password"))
                }
            }
        }
    }
}
