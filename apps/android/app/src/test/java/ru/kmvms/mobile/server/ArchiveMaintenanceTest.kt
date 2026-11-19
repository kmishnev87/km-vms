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
class ArchiveMaintenanceTest {
    private val id="e228ba7c-4a11-4367-a0bf-6ed44a3946fe"
    private fun api(server:MockWebServer)=ArchiveMaintenanceApi(NasApi(ServerAddress.parse(server.url("/").toString())).apply{useSession("fixture")})
    private class MemoryReceipts:ArchiveReceipts {
        var value:IntegrityAttempt?=null;var batchId:String?=null
        override fun attempt()=value
        override fun saveAttempt(value:IntegrityAttempt?){this.value=value}
        override fun batch()=batchId
        override fun saveBatch(value:String?){batchId=value}
    }
    @Test fun invalidLocalReceiptCannotCreateAnArchiveAction(){
        assertNull(IntegrityAttempt.parse("""{"kind":"scan","idempotency_key":"../other"}"""))
        assertNull(IntegrityAttempt.parse("""{"kind":"review","idempotency_key":"$id"}"""))
        val valid=IntegrityAttempt(id,"review",IntegrityPreview(id,"delete",setOf(id),"a".repeat(64),1,0,20))
        assertEquals(valid.copy(preview=valid.preview!!.copy(bytes=0)),IntegrityAttempt.parse(valid.json().toString()))
    }
    @Test fun deletionUsesThePreviewHashAndOnlyTheConfirmedObjectIds()=runBlocking {
        MockWebServer().use{server->
            val api=api(server)
            server.enqueue(MockResponse().setBody("""{"selection_hash":"${"a".repeat(64)}","eligible_count":1,"unavailable_count":0,"total_bytes":20}"""))
            val preview=api.preview(id,"delete",setOf(id));val request=server.takeRequest()
            assertEquals("/api/storage/integrity/scans/$id/actions/preview",request.path)
            assertFalse(JSONObject(request.body.readUtf8()).has("confirm"))
            server.enqueue(MockResponse().setBody("""{"batch_id":"$id","state":"prepared"}"""))
            api.apply(IntegrityAttempt(id,"review",preview));val body=JSONObject(server.takeRequest().body.readUtf8())
            assertTrue(body.getBoolean("confirm"));assertEquals("a".repeat(64),body.getString("selection_hash"))
            assertEquals(listOf(id),body.getJSONObject("selection").getJSONArray("ids").let{(0 until it.length()).map(it::getString)})
            assertFalse(body.getJSONObject("selection").has("filters"))
        }
    }
    @Test fun aLostScanReplyDoesNotLoseItsIdempotencyKeyOnReopening()=runBlocking {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try{MockWebServer().use{server->
            val posted=CopyOnWriteArrayList<String>()
            server.dispatcher=object:okhttp3.mockwebserver.Dispatcher(){override fun dispatch(request:RecordedRequest):MockResponse {
                if(request.method=="GET")return MockResponse().setBody("""{"status":"not_run"}""")
                val key=JSONObject(request.body.readUtf8()).getString("idempotency_key");posted.add(key)
                return if(posted.size==1)MockResponse().setResponseCode(503).setBody("{}")else MockResponse().setBody("""{"scan_id":"$id","status":"queued"}""")
            }}
            val store=MemoryReceipts();val first=ArchiveMaintenanceController(api(server),store)
            withTimeout(15000){first.state.first{!it.loading}}
            first.startScan();withTimeout(15000){first.state.first{!it.busy&&it.result=="unknown"}}
            val key=store.value!!.key;first.close(); withTimeout(5_000) { first.awaitClosed() }
            val reopened=ArchiveMaintenanceController(api(server),store)
            try{withTimeout(15000){reopened.state.first{!it.loading}}
                assertEquals(1,posted.size);assertEquals(key,reopened.state.value.pending!!.key)
                reopened.retry();withTimeout(15000){reopened.state.first{!it.busy&&it.pending==null}}
                assertEquals(listOf(key,key),posted.toList());assertNull(store.value)
            }finally{reopened.close(); withTimeout(5_000) { reopened.awaitClosed() }}
        }}finally{Dispatchers.resetMain()}
    }
    @Test fun aDefinitivelyRejectedScanDoesNotPermanentlyLockMaintenance()=runBlocking {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try{MockWebServer().use{server->
            server.dispatcher=object:okhttp3.mockwebserver.Dispatcher(){override fun dispatch(request:RecordedRequest)=
                if(request.method=="POST")MockResponse().setResponseCode(409).setBody("{}")else MockResponse().setBody("""{"status":"not_run"}""")}
            val store=MemoryReceipts();val controller=ArchiveMaintenanceController(api(server),store)
            try{withTimeout(15000){controller.state.first{!it.loading}}
                controller.startScan();withTimeout(15000){controller.state.first{!it.busy&&it.result=="failed"}}
                assertNull(store.value);assertNull(controller.state.value.pending)
            }finally{controller.close(); withTimeout(5_000) { controller.awaitClosed() }}
        }}finally{Dispatchers.resetMain()}
    }
}
