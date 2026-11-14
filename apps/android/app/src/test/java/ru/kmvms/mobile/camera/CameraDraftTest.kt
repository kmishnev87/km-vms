package ru.kmvms.mobile.camera

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi

class CameraDraftTest {
    private fun saved()=CameraDraft.from(JSONObject("""{"name":"Garden","protocol":"onvif","host":"192.168.1.20","port":80,"username":"fixture","password":"must-not-be-copied","enabled":true,"events_enabled":true,"rtsp_main_url":"/main","rtsp_sub_url":"/sub","default_live_stream":"main","default_record_stream":"sub"}"""))
    @Test fun ordinaryPolicyEditCannotResetCredentialsOrEventReception(){
        val baseline=saved()
        val payload=baseline.change("retention_days","14").payload(baseline=baseline)
        assertEquals(setOf("retention_days"),payload.keys().asSequence().toSet());assertEquals(14,payload.getInt("retention_days"))
        assertEquals("",baseline["password"]);assertFalse(baseline.toString().contains("must-not-be-copied"))
        assertFalse(baseline.connectionChanged(baseline.change("name","New name")))
    }
    @Test fun publicLiveAndRecordingPurposesDoNotAssumeMainAndSub(){
        val draft=saved()
        assertEquals("sub",draft["default_record_stream"]);assertEquals("main",draft["default_live_stream"])
        assertFalse(draft.change("enabled","false").connectionChanged(draft))
        assertTrue(draft.change("rtsp_sub_url","/other-sub").connectionChanged(draft))
    }
    @Test fun separateVideoAddressFollowsHostOnlyUntilExplicitlyOverridden(){
        val draft=saved().change("host","192.168.1.21")
        assertEquals("192.168.1.21",draft["rtsp_host"])
        assertEquals("192.168.2.10",draft.change("rtsp_host","192.168.2.10").change("host","192.168.1.22")["rtsp_host"])
    }
    @Test fun policyAndPortBoundsMatchServerWithoutTruncatingNames(){
        val draft=saved().change("name","П".repeat(255)).change("retention_days","36500")
        assertTrue(draft.validPolicy());assertFalse(draft.change("name","П".repeat(256)).validPolicy())
        assertFalse(draft.change("retention_days","36501").validPolicy());assertFalse(draft.change("storage_quota_gb","0").validPolicy())
        assertFalse(draft.change("port","0").validConnection(true));assertFalse(draft.change("rtsp_port","65536").validConnection(true))
    }
    @Test fun selectingSourceUsesItsOwnRecommendedProfilesNotTheirNames(){
        val first=CameraProfile("a","Main misleading","/a","first","1","H264","1920×1080","25")
        val second=first.copy(token="b",path="/b",source="second",channel="2")
        val sub=second.copy(token="c",path="/c")
        val group=CameraSource("second","", "2",setOf("b","c"),"b","c")
        val catalog=CameraCatalog(listOf(first,second,sub),listOf(group),"fixture-proof")
        val chosen=CameraEditorController.forSource(saved(),catalog,group)
        assertEquals("b",chosen["onvif_profile_token"]);assertEquals("c",chosen["onvif_sub_profile_token"])
        assertEquals("2",chosen["onvif_channel_id"]);assertEquals("/b",chosen["rtsp_main_url"])
        assertFalse(catalog.toString().contains("fixture-proof"))
    }
    @Test fun rtspModeDoesNotSubmitOnvifVideoAddressOverrides(){
        val payload=saved().change("protocol","rtsp").change("rtsp_main_url","/main").payload()
        assertFalse(payload.has("rtsp_host"));assertFalse(payload.has("rtsp_port"));assertEquals(554,payload.getInt("port"))
        assertFalse(payload.getBoolean("events_enabled"))
    }
    @Test fun savedCameraCredentialsStayOnNasDuringReadOnlyProbe()=runBlocking{
        MockWebServer().use{server->
            server.enqueue(MockResponse().setBody("""{"profiles":[],"source_groups":[],"onvif_probe_token":"fixture-proof"}"""))
            val api=CameraApi(NasApi(ServerAddress.parse(server.url("/").toString())).apply{useSession("fixture")})
            api.probe(saved(),22)
            val request=server.takeRequest();val body=JSONObject(request.body.readUtf8())
            assertEquals("/api/cameras/onvif/probe",request.path);assertEquals(22L,body.getLong("camera_id"))
            assertFalse(body.has("password"))
        }
    }
}
