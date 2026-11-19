package ru.kmvms.mobile.server

import android.content.Context
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import org.json.JSONObject
import ru.kmvms.mobile.data.NasException
import java.util.UUID
import java.nio.charset.StandardCharsets

data class BackupArtifact(val id:String,val created:String?,val bytes:Long,val validation:String?,val canDelete:Boolean)
data class BackupInventory(val items:List<BackupArtifact>,val total:Int,val more:Boolean) {
    companion object {fun from(value:JSONObject)=BackupInventory(rows(value.opt("artifacts")).map{
        BackupArtifact(it.getString("artifact_id"),it.text("artifact_created_at"),it.optLong("file_size"),it.text("restore_validation_status"),it.optBoolean("delete_supported"))
    },value.optInt("total_count"),value.optBoolean("has_more"))}
}
data class BackupAttempt(val submission:String,val kind:String,val artifact:String?) {
    fun json()=JSONObject().put("submission",submission).put("kind",kind).put("artifact",artifact).toString()
    companion object {fun parse(value:String?):BackupAttempt?=try{
        val json=JSONObject(value?:"");val submission=json.getString("submission");val kind=json.getString("kind")
        require(submission.matches(Regex("[a-f0-9-]{36}"))&&kind in setOf("create","validate","delete"))
        val artifact=json.text("artifact")
        require(kind=="create"||!artifact.isNullOrBlank()&&artifact.length<=200&&artifact.none{it=='/'||it=='\\'||it.code<32})
        BackupAttempt(submission,kind,artifact)
    }catch(_:Exception){null}}
}
data class BackupsState(val items:List<BackupArtifact> = emptyList(),val total:Int=0,val more:Boolean=false,
    val loading:Boolean=false,val pending:BackupAttempt?=null,val operationState:String?=null,val polling:Boolean=false,val failures:Int=0)

interface BackupAttemptStore {
    fun load():BackupAttempt?
    fun save(attempt:BackupAttempt?)
}
private class PreferencesBackupAttempts(context:Context,private val namespace:String):BackupAttemptStore {
    private val preferences=context.getSharedPreferences("server_operations",Context.MODE_PRIVATE)
    override fun load()=BackupAttempt.parse(preferences.getString(namespace,null))
    override fun save(attempt:BackupAttempt?){preferences.edit().putString(namespace,attempt?.json()).apply()}
}

/** A lost HTTP reply keeps the SAME receipt ID, including when this screen is reopened. */
class BackupsController(private val api:ServerApi,private val store:BackupAttemptStore) {
    constructor(context:Context,api:ServerApi,namespace:String):this(api,PreferencesBackupAttempts(context,namespace))
    private val scope=CoroutineScope(SupervisorJob()+Dispatchers.Main.immediate)
    private val mutable=MutableStateFlow(BackupsState(pending=store.load()))
    val state=mutable.asStateFlow()
    private var active=false
    private var posting=false
    private var poll:Job?=null
    init{refresh()}
    fun foreground(value:Boolean){active=value;if(value)watch()else{poll?.cancel();poll=null;mutable.update{it.copy(polling=false)}}}
    fun refresh(append:Boolean=false){
        if(mutable.value.loading)return
        mutable.update{it.copy(loading=true)}
        scope.launch{try{val inventory=api.backups(if(append)mutable.value.items.size else 0)
            mutable.update{it.copy(items=if(append)(it.items+inventory.items).distinctBy{row->row.id}else inventory.items,total=inventory.total,more=inventory.more)}
        }catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){failure()}finally{mutable.update{it.copy(loading=false)}}}
    }
    fun start(kind:String,artifact:String?=null){
        if(mutable.value.pending!=null||posting)return
        require(kind in setOf("create","validate","delete"));require(kind=="create"||!artifact.isNullOrBlank())
        val attempt=BackupAttempt(UUID.randomUUID().toString(),kind,artifact)
        store.save(attempt)
        mutable.update{it.copy(pending=attempt,operationState="running")}
        submit(attempt)
    }
    fun retry(){mutable.value.pending?.let{submit(it)}}
    private fun submit(attempt:BackupAttempt){
        if(posting)return
        posting=true;poll?.cancel();poll=null
        mutable.update{it.copy(operationState="running",polling=true)}
        scope.launch{try{
            val receipt=when(attempt.kind){"create"->api.createBackup(attempt.submission);"validate"->api.validateBackup(attempt.artifact!!,attempt.submission);else->api.deleteBackup(attempt.artifact!!,attempt.submission)}
            accept(attempt,receipt)
        }catch(cancelled:CancellationException){throw cancelled}catch(error:Exception){
            if((error as? NasException)?.httpStatus in setOf(400,401,403,404,422)){store.save(null);mutable.update{it.copy(pending=null,operationState="failed")};failure();refresh()}
            else mutable.update{it.copy(operationState="unknown")}
        }finally{posting=false;mutable.update{it.copy(polling=false)};watch()}}
    }
    fun check(){poll?.cancel();poll=null;watch()}
    private fun watch(){
        if(!active||posting||poll?.isActive==true)return
        val attempt=mutable.value.pending?:return
        poll=scope.launch{
            mutable.update{it.copy(polling=true)}
            var failures=0;val deadline=System.nanoTime()+10L*60*1_000_000_000
            try{while(isActive&&active&&mutable.value.pending==attempt&&System.nanoTime()<deadline){
                delay(2000)
                try{accept(attempt,api.backupOperation(attempt.submission));failures=0}
                catch(cancelled:CancellationException){throw cancelled}
                catch(_:Exception){failures++;if(failures>=4){mutable.update{it.copy(operationState="unknown")};break}}
            }
                if(mutable.value.pending==attempt&&System.nanoTime()>=deadline)mutable.update{it.copy(operationState="unknown")}
            }finally{mutable.update{it.copy(polling=false)}}
        }
    }
    private fun accept(attempt:BackupAttempt,receipt:JSONObject){
        if(mutable.value.pending!=attempt)return
        require(receipt.text("submission_id")==attempt.submission)
        val state=receipt.text("state")?:"unknown"
        mutable.update{it.copy(operationState=state)}
        if(state=="completed"&&attempt.kind=="create"){
            val artifact=receipt.text("artifact_id")?:receipt.optJSONObject("result")?.text("artifact_id")
            require(!artifact.isNullOrBlank())
            // Creation and disposable trial restore are separate NAS operations.
            // Persist a deterministic child receipt before sending it, so a lost
            // reply or reopening can retry the SAME validation rather than create
            // a second backup. Never call the current-database restore endpoint.
            val validation=BackupAttempt.parse(BackupAttempt(
                UUID.nameUUIDFromBytes(("km-vms-backup-validation:"+attempt.submission).toByteArray(StandardCharsets.UTF_8)).toString(),
                "validate",artifact).json())?:throw IllegalArgumentException("invalid_backup_receipt")
            store.save(validation)
            mutable.update{it.copy(pending=validation,operationState="running")}
            scope.launch{yield();submit(validation)}
            return
        }
        if(state in setOf("completed","failed","blocked","cancelled","interrupted")){
            store.save(null)
            mutable.update{it.copy(pending=null)}
            refresh()
        }
    }
    private fun failure(){mutable.update{it.copy(failures=it.failures+1)}}
    fun close(){scope.cancel()}
    internal suspend fun awaitClosed() { scope.coroutineContext[Job]?.join() }
}
