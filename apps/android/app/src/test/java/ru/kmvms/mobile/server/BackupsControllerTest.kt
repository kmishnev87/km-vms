package ru.kmvms.mobile.server

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.withTimeout
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import java.util.concurrent.CopyOnWriteArrayList

@OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
class BackupsControllerTest {
    private class MemoryStore(var attempt:BackupAttempt?=null):BackupAttemptStore {
        override fun load()=attempt
        override fun save(attempt:BackupAttempt?){this.attempt=attempt}
    }
    private fun client(server:MockWebServer)=ServerApi(NasApi(ServerAddress.parse(server.url("/").toString())).apply{useSession("fixture")})
    @Test fun failedReplyAndReopeningNeverCreateAFreshAttemptId()=runBlocking {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try{MockWebServer().use{server->
            val submissions=CopyOnWriteArrayList<String>()
            server.dispatcher=object:Dispatcher(){override fun dispatch(request:RecordedRequest):MockResponse {
                if(request.method=="GET")return MockResponse().setBody("""{"artifacts":[],"total_count":0,"has_more":false}""")
                val id=JSONObject(request.body.readUtf8()).getString("submission_id");submissions.add(id)
                return if(submissions.size==1)MockResponse().setResponseCode(503).setBody("{}")
                    else MockResponse().setBody(JSONObject().put("submission_id",id).put("state","completed").put("artifact_id","own-backup").toString())
            }}
            val store=MemoryStore()
            val first=BackupsController(client(server),store)
            withTimeout(15000){first.state.first{!it.loading}}
            first.start("create")
            withTimeout(15000){first.state.first{it.operationState=="unknown"&&!it.polling}}
            val original=store.attempt!!.submission
            first.close(); withTimeout(5_000) { first.awaitClosed() }
            val reopened=BackupsController(client(server),store)
            try{
                withTimeout(15000){reopened.state.first{!it.loading}}
                assertEquals(1,submissions.size)
                assertEquals(original,reopened.state.value.pending!!.submission)
                reopened.retry()
                withTimeout(15000){reopened.state.first{it.pending==null&&it.operationState=="completed"}}
                assertEquals(listOf(original,original),submissions.take(2));assertEquals(3,submissions.size)
                assertNotEquals(original,submissions.last());assertNull(store.attempt)
            }finally{reopened.close(); withTimeout(5_000) { reopened.awaitClosed() }}
        }}finally{Dispatchers.resetMain()}
    }
    @Test fun invalidStoredAttemptCannotBecomeAnAutomaticDestructiveRequest(){
        assertNull(BackupAttempt.parse("""{"submission":"not-an-id","kind":"delete","artifact":"one"}"""))
        assertNull(BackupAttempt.parse("""{"submission":"00000000-0000-0000-0000-000000000000","kind":"delete","artifact":"../other"}"""))
        assertNull(BackupAttempt.parse("""{"submission":"00000000-0000-0000-0000-000000000000","kind":"validate"}"""))
    }
    @Test fun savedAttemptContainsNoNasCredentials(){
        val attempt=BackupAttempt("00000000-0000-0000-0000-000000000000","create",null)
        val body=JSONObject(attempt.json())
        assertFalse(body.has("password"));assertFalse(body.has("access_token"));assertEquals(attempt,BackupAttempt.parse(attempt.json()))
    }
    @Test fun createIncludesDisposableTrialRestoreBeforeReportingCompleted()=runBlocking {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try{MockWebServer().use{server->
            val paths=CopyOnWriteArrayList<String>()
            server.dispatcher=object:Dispatcher(){override fun dispatch(request:RecordedRequest):MockResponse {
                if(request.method=="GET")return MockResponse().setBody("""{"artifacts":[],"total_count":0,"has_more":false}""")
                val body=JSONObject(request.body.readUtf8());val id=body.getString("submission_id")
                paths.add(request.path!!)
                if(request.path=="/api/system/restore/apply")assertEquals("fixture-backup",body.getString("artifact_id"))
                return MockResponse().setBody(JSONObject().put("submission_id",id).put("state","completed").put("artifact_id","fixture-backup").toString())
            }}
            val store=MemoryStore();val controller=BackupsController(client(server),store)
            try{
                withTimeout(15000){controller.state.first{!it.loading}}
                controller.start("create")
                withTimeout(15000){controller.state.first{it.operationState=="completed"&&it.pending==null}}
                assertEquals(listOf("/api/system/backup/create","/api/system/restore/apply"),paths.toList())
                assertNull(store.attempt)
            }finally{controller.close(); withTimeout(5_000) { controller.awaitClosed() }}
        }}finally{Dispatchers.resetMain()}
    }
}
