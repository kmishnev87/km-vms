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
    private suspend fun NasUpdatesController.closeForTest() {
        close()
        withTimeout(5_000) { awaitClosed() }
    }
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
                if(request.path=="/api/system/update/check")return MockResponse().setBody(available)
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
            val id=store.value!!.id;first.closeForTest()
            val second=NasUpdatesController(api(server),store)
            try{
                withTimeout(15000){second.state.first{!it.loading}}
                assertEquals(id,second.state.value.pending!!.id);assertEquals(1,posted.size)
                second.retry()
                withTimeout(15000){second.state.first{!it.posting&&it.pending==null}}
                assertEquals(listOf(id,id),posted.toList());assertTrue(second.state.value.progress!!.completed(null))
                assertNull(store.value);assertFalse(second.state.value.watching)
            }finally{second.closeForTest()}
        }}finally{Dispatchers.resetMain()}
    }
    @Test fun aDefinitiveRejectionReloadsAvailabilityInsteadOfKeepingAStaleApplyButton()=runBlocking {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try{MockWebServer().use{server->
            var rejected=false
            server.dispatcher=object:Dispatcher(){override fun dispatch(request:RecordedRequest):MockResponse {
                if(request.path=="/api/system/update/check")return MockResponse().setBody(available)
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
            }finally{controller.closeForTest()}
        }}finally{Dispatchers.resetMain()}
    }

    @Test fun openingChecksTheOfficialReleaseOnceButDoesNotPollAnIdleServer()=runBlocking {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try { MockWebServer().use { server ->
            val paths=CopyOnWriteArrayList<String>()
            server.dispatcher=object:Dispatcher(){override fun dispatch(request:RecordedRequest):MockResponse {
                paths.add("${request.method} ${request.path}")
                return MockResponse().setBody(if(request.path=="/api/system/update/apply/status") """{"status":"idle"}""" else available)
            }}
            val controller=NasUpdatesController(api(server),MemoryStore())
            try {
                withTimeout(15000){controller.state.first{!it.loading}}
                assertEquals(1,paths.count{it=="POST /api/system/update/check"})
                assertEquals(candidate,controller.state.value.check?.candidate)
                controller.foreground(true)
                withTimeout(15000){controller.state.first{!it.loading}}
                assertEquals(1,paths.count{it=="POST /api/system/update/check"})
                assertFalse(controller.state.value.watching)
                assertTrue(paths.none{it=="POST /api/system/update/apply"})
            } finally { controller.closeForTest() }
        }} finally { Dispatchers.resetMain() }
    }

    @Test fun statusButtonFetchesFreshProgressAndAnExplicitReceiptNotAnotherReleaseCheck()=runBlocking {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try { MockWebServer().use { server ->
            val phase=java.util.concurrent.atomic.AtomicReference("building_images")
            val reject=java.util.concurrent.atomic.AtomicBoolean(false)
            val paths=CopyOnWriteArrayList<String>()
            server.dispatcher=object:Dispatcher(){override fun dispatch(request:RecordedRequest):MockResponse {
                paths.add(request.path.orEmpty())
                if(request.path=="/api/system/update/apply/status") {
                    if(reject.get())return MockResponse().setResponseCode(503).setBody("{}")
                    return MockResponse().setBody("""{"status":"staging","current_step":"preflight","phase":"${phase.get()}","elapsed_seconds":281}""")
                }
                return MockResponse().setBody(available)
            }}
            val controller=NasUpdatesController(api(server),MemoryStore())
            try {
                withTimeout(15000){controller.state.first{!it.loading}}
                assertEquals("building_images",controller.state.value.progress?.phase)
                val checks=paths.count{it=="/api/system/update/check"}
                phase.set("verifying_images");controller.refreshStatus()
                withTimeout(15000){controller.state.first{!it.loading}}
                assertEquals("verifying_images",controller.state.value.progress?.phase)
                assertNotNull(controller.state.value.statusCheckedAt)
                assertEquals(checks,paths.count{it=="/api/system/update/check"})
                reject.set(true);controller.refreshStatus()
                withTimeout(15000){controller.state.first{!it.loading}}
                assertTrue(controller.state.value.statusCheckFailed)
                assertEquals("verifying_images",controller.state.value.progress?.phase)
            } finally { controller.closeForTest() }
        }} finally { Dispatchers.resetMain() }
    }

    @Test fun detailedPhasesAndMeasuredBytesAreNotAnInventedOverallPercentage() {
        val payload=JSONObject("""{"status":"staging","phase":"building_images","current_step":"preflight","elapsed_seconds":281,"target_version":"0.9.18","steps":[{"name":"request","status":"completed"},{"name":"preflight","status":"running"}]}""")
        val value=NasUpdateProgress.from(payload)
        assertEquals("building_images",value.phase);assertEquals("preflight",value.step)
        assertNull(value.percent);assertEquals(2,value.steps.size)
        payload.put("phase","downloading").put("progress_current",250).put("progress_total",1000)
            .put("progress_unit","bytes").put("progress_percent",25)
        assertEquals(25,NasUpdateProgress.from(payload).percent)
        assertEquals(250L,NasUpdateProgress.from(payload).current)
        for ((key, invalid) in listOf("progress_current" to -1,"progress_total" to 0,"progress_percent" to 98,"progress_unit" to "seconds")) {
            assertNull(key,NasUpdateProgress.from(JSONObject(payload.toString()).put(key,invalid)).percent)
        }
    }
}
