package ru.kmvms.mobile.server

import kotlinx.coroutines.*
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.test.resetMain
import okhttp3.mockwebserver.*
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import java.util.concurrent.CopyOnWriteArrayList

@OptIn(ExperimentalCoroutinesApi::class)
class NasUpdatesTest {
    private val commit="a".repeat(40)
    private val candidate get()=NasUpdateCandidate("0.9.16",commit)
    private val available get()="""{"status":"update_available","can_apply_from_ui":true,"installed_release":{"version":"0.9.15"},"latest":{"version":"0.9.16","commit":"$commit"}}"""
    private class MemoryStore(var value:NasUpdateAttempt?=null):NasUpdateAttempts {
        override fun load()=value
        override fun save(value:NasUpdateAttempt?){this.value=value}
    }
    private fun api(server:MockWebServer)=NasUpdatesApi(NasApi(ServerAddress.parse(server.url("/").toString())).apply{useSession("fixture")})
    @Test fun onlyTheNasCanAuthorizeAnUpdateCandidate(){
        assertNull(NasUpdateCheck.from(JSONObject(available).put("can_apply_from_ui",false)).candidate)
        assertEquals(candidate,NasUpdateCheck.from(JSONObject(available)).candidate)
        assertNull(NasUpdateCheck.from(JSONObject(available).getJSONObject("latest").put("commit","bad")).candidate)
        val trusted=JSONObject(available).put("can_apply_from_ui",false).put("trusted_apply_candidate",JSONObject(available))
        assertEquals(candidate,NasUpdateCheck.from(trusted).candidate)
    }
    @Test fun aGreenFinalStatusRequiresTheInstalledCommitNotJustAnEndedHttpCall(){
        assertFalse(NasUpdateProgress.from(JSONObject("""{"status":"idle"}""")).visible)
        val attempt=NasUpdateAttempt("e228ba7c-4a11-4367-a0bf-6ed44a3946fe",candidate)
        assertFalse(NasUpdateProgress.from(JSONObject("""{"status":"completed","commit_verified":false}""")).completed(attempt))
        assertFalse(NasUpdateProgress.from(JSONObject("""{"status":"completed","commit_verified":true,"installed_commit":"${"b".repeat(40)}"}""")).completed(attempt))
        assertTrue(NasUpdateProgress.from(JSONObject("""{"status":"completed","commit_verified":true,"installed_commit":"$commit"}""")).completed(attempt))
        assertEquals(attempt,NasUpdateAttempt.parse(attempt.json().toString()))
        assertNull(NasUpdateAttempt.parse("{}"))
    }
    @Test fun lostReplyAndReopenedScreenRetryTheSameReceiptWithoutIdlePolling()=runBlocking {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try{MockWebServer().use{server->
            val posted=CopyOnWriteArrayList<String>()
            server.dispatcher=object:Dispatcher(){override fun dispatch(request:RecordedRequest):MockResponse {
                if(request.path=="/api/system/update/status")return MockResponse().setBody(available)
                if(request.method=="GET")return MockResponse().setBody("""{"status":"idle"}""")
                val body=JSONObject(request.body.readUtf8());val id=body.getString("submission_id");posted.add(id)
                assertTrue(body.getBoolean("confirm"));assertEquals(commit,body.getString("expected_manifest_commit"))
                assertFalse(body.has("url"));assertFalse(body.has("command"))
                return if(posted.size==1)MockResponse().setResponseCode(503).setBody("{}") else MockResponse().setBody("""{"status":"completed","submission_id":"$id","commit_verified":true,"installed_commit":"$commit"}""")
            }}
            val store=MemoryStore();val first=NasUpdatesController(api(server),store)
            withTimeout(15000){first.state.first{!it.loading}}
            first.start(candidate)
            withTimeout(15000){first.state.first{!it.posting&&it.reconnecting}}
            val id=store.value!!.id;first.close()
            val second=NasUpdatesController(api(server),store)
            try{
                withTimeout(15000){second.state.first{!it.loading}}
                assertEquals(id,second.state.value.pending!!.id);assertEquals(1,posted.size)
                second.retry()
                withTimeout(15000){second.state.first{!it.posting&&it.pending==null}}
                assertEquals(listOf(id,id),posted.toList());assertTrue(second.state.value.progress!!.completed(null))
                assertNull(store.value);assertFalse(second.state.value.watching)
            }finally{second.close()}
        }}finally{Dispatchers.resetMain()}
    }
    @Test fun aDefinitiveRejectionReloadsAvailabilityInsteadOfKeepingAStaleApplyButton()=runBlocking {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try{MockWebServer().use{server->
            var rejected=false
            server.dispatcher=object:Dispatcher(){override fun dispatch(request:RecordedRequest):MockResponse {
                if(request.method=="POST"){rejected=true;return MockResponse().setResponseCode(409).setBody("{}")}
                return MockResponse().setBody(if(request.path=="/api/system/update/status"){
                    if(rejected)"""{"status":"blocked","can_apply_from_ui":false}"""else available
                }else """{"status":"idle"}""")
            }}
            val store=MemoryStore();val controller=NasUpdatesController(api(server),store)
            try{withTimeout(15000){controller.state.first{!it.loading}}
                controller.start(candidate)
                withTimeout(15000){controller.state.first{!it.loading&&!it.posting&&it.check?.status=="blocked"}}
                assertNull(controller.state.value.check?.candidate);assertNull(controller.state.value.pending);assertNull(store.value)
            }finally{controller.close()}
        }}finally{Dispatchers.resetMain()}
    }
}
