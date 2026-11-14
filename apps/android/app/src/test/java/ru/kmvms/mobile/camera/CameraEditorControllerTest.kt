package ru.kmvms.mobile.camera

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.withTimeout
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi

@OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
class CameraEditorControllerTest {
    private fun client(server:MockWebServer)=CameraApi(NasApi(ServerAddress.parse(server.url("/").toString())).apply{useSession("fixture")})
    private fun MockWebServer.json(body:String){enqueue(MockResponse().setBody(body))}
    private fun MockWebServer.verification(probe:Boolean){
        if(probe){
            json("""{"onvif_probe_token":"fixture-management-proof","profiles":[
                {"token":"m","stream_path":"/main","source_id":"lens-a","video":{"codec":"H264","width":1920,"height":1080,"fps":25}},
                {"token":"s1","stream_path":"/sub-one","source_id":"lens-a","video":{"codec":"H264","width":640,"height":360,"fps":25}},
                {"token":"s2","stream_path":"/sub-two","source_id":"lens-a","video":{"codec":"H264","width":1280,"height":720,"fps":25}}],
                "source_groups":[{"id":"lens-a","channel":1,"profile_tokens":["m","s1","s2"],"main_profile_token":"m","sub_profile_token":"s1"}]}""")
            json("""{"supported":true}""")
        }
        for(role in listOf("main","sub"))json("""{"ok":true,"tested_role":"$role","video":{"codec":"h264"},"validation_token":"fixture-$role-proof"}""")
        repeat(3){json("""{"config":{"codec":"H264","resolution":"640x360","fps":25},"supported":{}}""")}
    }
    @Test fun changingSelectedStreamKeepsManagementProofButRenewsVideoProofs()=runBlocking {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try{MockWebServer().use{server->
            server.verification(true);server.verification(false);server.json("""{"id":10}""")
            val editor=CameraEditorController(client(server),null)
            try{
                editor.change("name","Fixture");editor.change("host","192.168.1.10")
                editor.change("username","fixture");editor.change("password","fixture-password")
                editor.check()
                withTimeout(15000){editor.state.first{it.verified&&!it.busy}}
                editor.purpose("live",editor.state.value.catalog!!.profiles.single{it.token=="s2"})
                withTimeout(15000){editor.state.first{it.verified&&!it.busy}}
                editor.save()
                withTimeout(15000){editor.state.first{it.saved&&!it.busy}}
                val requests=(0 until server.requestCount).map{server.takeRequest()}
                assertEquals(1,requests.count{it.path=="/api/cameras/onvif/probe"})
                val body=JSONObject(requests.single{it.path=="/api/cameras"&&it.method=="POST"}.body.readUtf8())
                assertEquals("fixture-management-proof",body.getString("onvif_probe_token"))
                assertEquals("fixture-sub-proof",body.getString("sub_validation_token"))
                assertEquals("s2",body.getString("onvif_sub_profile_token"))
                assertEquals("lens-a",editor.state.value.source)
            }finally{editor.close()}
        }}finally{Dispatchers.resetMain()}
    }
    @Test fun editingOnlyRetentionDoesNotProbeTheCameraOrResetEvents()=runBlocking {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try{MockWebServer().use{server->
            server.json("""{"name":"Fixture","protocol":"onvif","host":"192.168.1.10","port":80,"username":"fixture","rtsp_main_url":"/main","events_enabled":true,"retention_days":30}""")
            server.json("""{"id":10}""")
            val editor=CameraEditorController(client(server),10)
            try{
                withTimeout(15000){editor.state.first{!it.busy}}
                editor.change("retention_days","14");editor.save()
                withTimeout(15000){editor.state.first{it.saved&&!it.busy}}
                assertEquals("GET",server.takeRequest().method)
                val request=server.takeRequest();assertEquals("PUT",request.method)
                val body=JSONObject(request.body.readUtf8())
                assertEquals(setOf("retention_days"),body.keys().asSequence().toSet())
                assertEquals(2,server.requestCount)
            }finally{editor.close()}
        }}finally{Dispatchers.resetMain()}
    }
}
