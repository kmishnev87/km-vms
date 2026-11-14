package ru.kmvms.mobile.media

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.core.PtzCommand
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.ptz.PtzApi

class PtzApiTest {
    @Test fun commandUsesTheSelectedLensWithoutAnotherCapabilityProbe() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"executed":true,"movement_mode":"continuous","lease_seconds":1.5}"""))
            val api = NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }
            val result = PtzApi(api).send(12, "profile-for-lens-2", PtzCommand("move", "left", .08, "fixture-control", 2))
            assertTrue(result.executed); assertTrue(result.continuous)
            val request = server.takeRequest()
            assertEquals("/api/cameras/12/onvif/ptz/command", request.path)
            val body = JSONObject(request.body.readUtf8())
            assertEquals("profile-for-lens-2", body.getString("profile_token")); assertTrue(body.getBoolean("hold"))
            assertEquals(2, body.getInt("sequence")); assertFalse(body.getBoolean("dry_run"))
            assertEquals(1, server.requestCount)
        }
    }
    @Test fun areaIsTrackedAndStopDoesNotRenewMovement() = runBlocking {
        MockWebServer().use { server ->
            repeat(2) { server.enqueue(MockResponse().setBody("""{"executed":true,"camera_stopped":true}""")) }
            val api = NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }
            val ptz = PtzApi(api)
            ptz.send(12, "selected", PtzCommand("area_zoom", controlId="fixture", sequence=1, coordinates=listOf(.1,.2,.7,.8)))
            ptz.send(12, "selected", PtzCommand("stop", controlId="fixture", sequence=2))
            val area = JSONObject(server.takeRequest().body.readUtf8())
            val stop = JSONObject(server.takeRequest().body.readUtf8())
            assertEquals(.7, area.getDouble("right"), .0001); assertEquals("fixture", stop.getString("control_id"))
            assertFalse(stop.has("hold")); assertFalse(stop.has("direction")); assertEquals(2, stop.getInt("sequence"))
        }
    }
    @Test fun unknownCapabilityDoesNotBecomeFalseUnsupported() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"supported":null,"profile_token":null}"""))
            val api = NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture") }
            val capability = PtzApi(api).capabilities(12)
            assertNull(capability.supported); assertNull(capability.profile)
        }
    }
}
