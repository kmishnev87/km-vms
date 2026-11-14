package ru.kmvms.mobile.server

import android.content.Context
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import org.json.JSONArray
import org.json.JSONObject
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.data.NasException
import java.util.UUID

data class IntegrityFinding(val id:String,val camera:String,val filename:String,val category:String,val actions:Set<String>)
data class IntegrityScan(val id:String?,val status:String,val stale:Boolean,val checked:Long,val found:Long) {
    val running get()=!stale&&status in setOf("queued","prepared","running","cancel_requested","interrupted")
    companion object {fun from(body:JSONObject)=IntegrityScan(body.text("scan_id"),body.text("status")?:"not_run",body.optBoolean("stale"),
        body.optJSONObject("progress")?.optLong("checked_count")?:0,body.optJSONObject("progress")?.optLong("found_count")?:0)}
}
data class IntegrityPage(val items:List<IntegrityFinding>,val total:Int,val next:String?) {
    companion object {fun from(body:JSONObject)=IntegrityPage(rows(body.opt("items")).map { row ->
        IntegrityFinding(validArchiveId(row.getString("finding_id")),row.text("camera_name").orEmpty(),row.text("display_name").orEmpty(),row.text("category").orEmpty(),
            row.optJSONArray("actions")?.let{(0 until it.length()).map(it::getString).filter{it in integrityActions}.toSet()}.orEmpty())
    },body.optInt("total_count"),body.text("next_cursor"))}
}
val integrityActions=setOf("recheck","repair","acknowledge","delete","reopen")
private fun validArchiveId(value:String)=value.also{require(UUID.fromString(it).toString()==it)}
data class IntegrityPreview(val scan:String,val action:String,val ids:Set<String>,val hash:String,val eligible:Int,val unavailable:Int,val bytes:Long) {
    fun request()=JSONObject().put("action",action).put("selection",JSONObject().put("ids",JSONArray(ids.toList())))
}
data class IntegrityAttempt(val key:String,val kind:String,val preview:IntegrityPreview?=null) {
    fun json()=JSONObject().put("idempotency_key",key).put("kind",kind).apply{preview?.let{
        put("scan",it.scan);put("action",it.action);put("ids",JSONArray(it.ids.toList()));put("hash",it.hash)
    }}
    companion object {fun parse(text:String?):IntegrityAttempt?=runCatching{
        val body=JSONObject(text?:"");val key=validArchiveId(body.getString("idempotency_key"));val kind=body.getString("kind")
        require(kind in setOf("scan","review"))
        val preview=if(kind=="scan")null else {
            val ids=body.getJSONArray("ids").let{array->(0 until array.length()).map{validArchiveId(array.getString(it))}.toSet()}
            require(ids.size in 1..200);val action=body.getString("action").also{require(it in integrityActions)}
            val hash=body.getString("hash").also{require(it.matches(Regex("[a-f0-9]{64}")))}
            IntegrityPreview(validArchiveId(body.getString("scan")),action,ids,hash,ids.size,0,0)
        }
        IntegrityAttempt(key,kind,preview)
    }.getOrNull()}
}
class ArchiveMaintenanceApi(private val api:NasApi) {
    suspend fun latest()=IntegrityScan.from(api.json("api/storage/integrity/scans/latest") as JSONObject)
    suspend fun findings(scan:String,cursor:String?=null)=IntegrityPage.from(api.json("api/storage/integrity/scans/${validArchiveId(scan)}/findings?limit=50"+
        (cursor?.let{"&cursor=${ru.kmvms.mobile.archive.encode(it)}"}?:"")) as JSONObject)
    suspend fun scan(key:String)=IntegrityScan.from(api.json("api/storage/integrity/scans","POST",JSONObject().put("idempotency_key",validArchiveId(key))) as JSONObject)
    suspend fun preview(scan:String,action:String,ids:Set<String>):IntegrityPreview {
        require(action in integrityActions&&ids.size in 1..200);ids.forEach(::validArchiveId)
        val request=JSONObject().put("action",action).put("selection",JSONObject().put("ids",JSONArray(ids.toList())))
        val value=api.json("api/storage/integrity/scans/${validArchiveId(scan)}/actions/preview","POST",request) as JSONObject
        val hash=value.getString("selection_hash").also{require(it.matches(Regex("[a-f0-9]{64}")))}
        return IntegrityPreview(scan,action,ids.toSet(),hash,value.getInt("eligible_count"),value.getInt("unavailable_count"),value.optLong("total_bytes"))
    }
    suspend fun apply(attempt:IntegrityAttempt):JSONObject {
        val value=requireNotNull(attempt.preview)
        return api.json("api/storage/integrity/scans/${validArchiveId(value.scan)}/actions","POST",value.request()
            .put("selection_hash",value.hash).put("idempotency_key",validArchiveId(attempt.key)).put("confirm",true)) as JSONObject
    }
    suspend fun batch(id:String)=api.json("api/storage/integrity/actions/${validArchiveId(id)}") as JSONObject
}
data class ArchiveMaintenanceState(val scan:IntegrityScan?=null,val items:List<IntegrityFinding> = emptyList(),val total:Int=0,val next:String?=null,
    val selected:Set<String> = emptySet(),val loading:Boolean=false,val busy:Boolean=false,val preview:IntegrityPreview?=null,
    val pending:IntegrityAttempt?=null,val batch:String?=null,val result:String?=null,val failures:Int=0)
interface ArchiveReceipts {
    fun attempt():IntegrityAttempt?
    fun saveAttempt(value:IntegrityAttempt?)
    fun batch():String?
    fun saveBatch(value:String?)
}
private class SavedArchiveReceipts(context:Context,private val namespace:String):ArchiveReceipts {
    private val preferences=context.getSharedPreferences("archive_operation_receipts",Context.MODE_PRIVATE)
    override fun attempt()=IntegrityAttempt.parse(preferences.getString(namespace,null))
    override fun saveAttempt(value:IntegrityAttempt?){preferences.edit().putString(namespace,value?.json()?.toString()).apply()}
    override fun batch()=preferences.getString("$namespace:batch",null)?.let{runCatching{validArchiveId(it)}.getOrNull()}
    override fun saveBatch(value:String?){preferences.edit().putString("$namespace:batch",value).apply()}
}
class ArchiveMaintenanceController(private val api:ArchiveMaintenanceApi,private val receipts:ArchiveReceipts) {
    constructor(context:Context,api:ArchiveMaintenanceApi,namespace:String):this(api,SavedArchiveReceipts(context,namespace))
    private val scope=CoroutineScope(SupervisorJob()+Dispatchers.Main.immediate)
    private val mutable=MutableStateFlow(ArchiveMaintenanceState(pending=receipts.attempt(),batch=receipts.batch()))
    val state=mutable.asStateFlow()
    private var active=false
    private var poll:Job?=null
    init{refresh()}
    fun foreground(value:Boolean){active=value;if(value){refresh();watch()}else{poll?.cancel();poll=null}}
    private fun save(value:IntegrityAttempt?){receipts.saveAttempt(value);mutable.update{it.copy(pending=value)}}
    fun refresh(append:Boolean=false){
        if(mutable.value.loading)return
        mutable.update{it.copy(loading=true)}
        scope.launch{try{
            val scan=api.latest();val current=mutable.value
            val page=scan.id?.takeIf{!scan.running&&!scan.stale}?.let{api.findings(it,if(append)current.next else null)}
            mutable.update{it.copy(scan=scan,items=if(page==null)emptyList()else if(append&&current.scan?.id==scan.id)(current.items+page.items).distinctBy{row->row.id}else page.items,
                total=page?.total?:0,next=page?.next,selected=if(current.scan?.id==scan.id)it.selected else emptySet())}
        }catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){failure()}finally{mutable.update{it.copy(loading=false)};watch()}}
    }
    fun select(id:String,value:Boolean){mutable.update{it.copy(selected=if(value)(it.selected+id).take(200).toSet()else it.selected-id)}}
    fun preview(action:String){
        val current=mutable.value;val scan=current.scan?.id?:return
        if(current.selected.isEmpty()||current.busy||current.pending!=null)return
        mutable.update{it.copy(busy=true)}
        scope.launch{try{val preview=api.preview(scan,action,current.selected);mutable.update{it.copy(preview=preview)}}
            catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){failure()}finally{mutable.update{it.copy(busy=false)}}}
    }
    fun dismissPreview(){mutable.update{it.copy(preview=null)}}
    fun startScan(){if(mutable.value.pending==null&&!mutable.value.busy){val attempt=IntegrityAttempt(UUID.randomUUID().toString(),"scan");save(attempt);submit(attempt)}}
    fun confirm(){val preview=mutable.value.preview?:return;if(mutable.value.pending!=null)return
        val attempt=IntegrityAttempt(UUID.randomUUID().toString(),"review",preview);save(attempt);mutable.update{it.copy(preview=null)};submit(attempt)}
    fun retry(){mutable.value.pending?.let(::submit)}
    private fun submit(attempt:IntegrityAttempt){
        if(mutable.value.busy)return
        mutable.update{it.copy(busy=true,result="running")}
        scope.launch{try{
            if(attempt.kind=="scan"){
                val scan=api.scan(attempt.key);save(null);mutable.update{it.copy(scan=scan,selected=emptySet(),items=emptyList(),result=null)};refresh()
            }else{
                val result=api.apply(attempt);val batch=validArchiveId(result.getString("batch_id"))
                receipts.saveBatch(batch);save(null)
                mutable.update{it.copy(batch=batch,result=result.text("state"),selected=emptySet())}
            }
        }catch(cancelled:CancellationException){throw cancelled}catch(error:Exception){
            val rejected=(error as? NasException)?.httpStatus in setOf(400,401,403,404,409,422)
            if(rejected)save(null)
            mutable.update{it.copy(result=if(rejected)"failed"else"unknown")};failure()
            if(rejected)refresh()
        }
        finally{mutable.update{it.copy(busy=false)};watch()}}
    }
    private fun watch(){
        if(!active||poll?.isActive==true)return
        val batch=mutable.value.batch
        if(batch==null&&mutable.value.scan?.running!=true)return
        poll=scope.launch{val deadline=System.nanoTime()+10L*60*1_000_000_000
            try{while(isActive&&active&&System.nanoTime()<deadline){
                delay(3000)
                if(batch!=null){
                    val result=try{api.batch(batch)}catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){mutable.update{it.copy(result="unknown")};break}
                    val status=result.text("state")?:"unknown";mutable.update{it.copy(batch=batch,result=status)}
                    if(status in setOf("completed","partial","blocked","failed","cancelled","interrupted")){
                        receipts.saveBatch(null);mutable.update{it.copy(batch=null)};refresh();break
                    }
                }else{refresh();if(mutable.value.scan?.running!=true)break}
            }}finally{poll=null}
        }
    }
    private fun failure(){mutable.update{it.copy(failures=it.failures+1)}}
    fun close(){scope.cancel()}
}
