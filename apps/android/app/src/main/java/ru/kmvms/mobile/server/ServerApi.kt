package ru.kmvms.mobile.server

import org.json.JSONArray
import org.json.JSONObject
import ru.kmvms.mobile.data.NasApi
import java.time.ZoneId
import java.net.URLEncoder

internal fun JSONObject.text(key:String)=optString(key).takeUnless{it.isBlank()||it=="null"}
internal fun JSONObject.number(key:String):Long?=if(has(key)&&!isNull(key))optLong(key).coerceAtLeast(0)else null
internal fun rows(value:Any?):List<JSONObject> = when(value){
    is JSONArray->(0 until value.length()).mapNotNull{value.optJSONObject(it)}
    is JSONObject->value.keys().asSequence().mapNotNull{value.optJSONObject(it)}.toList()
    else->emptyList()
}
private fun codes(value:JSONArray?):List<String> = if(value==null)emptyList()else(0 until value.length()).map{value.optString(it)}.filter{it.matches(Regex("[a-zA-Z0-9_.-]{1,96}"))}
data class StorageCamera(val id:Long,val name:String,val deleted:Boolean,val bytes:Long,val count:Long)
data class ArchiveRoot(val id:String,val label:String,val path:String?,val active:Boolean,val readable:Boolean,val bytes:Long,val requiresActivation:Boolean)
data class StorageView(val available:Boolean,val total:Long?,val free:Long?,val archiveBytes:Long,val cameras:List<StorageCamera>,val roots:List<ArchiveRoot>,val problems:Int) {
    companion object {fun from(value:JSONObject):StorageView {
        val capacity=value.optJSONObject("capacity")?:JSONObject()
        return StorageView(value.optBoolean("available"),capacity.number("total_bytes"),capacity.number("free_bytes"),
            value.optJSONObject("owned_archive")?.optLong("kmvms_owned_archive_bytes")?:0,
            rows(value.opt("camera_usage")).map{StorageCamera(it.optLong("camera_id"),it.optString("camera_name"),it.optBoolean("camera_deleted"),it.optLong("size_bytes"),it.optLong("recording_count"))},
            rows(value.opt("archive_roots")).map{ArchiveRoot(it.getString("id"),it.text("label").orEmpty(),it.text("host_path"),it.optBoolean("is_active"),it.optString("read_access_state")=="available",it.optLong("size_bytes"),it.optBoolean("requires_activation"))},
            value.optJSONObject("reconciliation_summary")?.optInt("problem_file_count")?:0)
    }}
}
data class SystemIssue(val cameraId:Long?,val camera:String?,val severity:String,val reasons:List<String>)
data class SystemDomain(val key:String,val severity:String,val reasons:List<String>,val issues:List<SystemIssue>,val idle:Boolean=false)
data class SystemView(val severity:String,val problems:Int,val warnings:Int,val unknown:Int,val domains:List<SystemDomain>) {
    companion object { fun from(value:JSONObject):SystemView {
        val domains=value.optJSONObject("domains")?:JSONObject();val summary=value.optJSONObject("summary")?:JSONObject()
        return SystemView(value.text("severity")?:"unknown",summary.optInt("problem_count"),summary.optInt("warning_count"),summary.optInt("unknown_count"),
            listOf("cameras","live","recorder","storage","retention","reconciliation").mapNotNull{key->domains.optJSONObject(key)?.let{domain->
                SystemDomain(key,domain.text("severity")?:"unknown",codes(domain.optJSONArray("reason_codes")?:domain.optJSONArray("safe_reason_codes")),
                    rows(domain.opt("items")).filter{it.optString("severity")!="ok"}.map{item->
                        SystemIssue(item.number("camera_id"),item.text("camera_name")?:item.text("name"),item.text("severity")?:"unknown",codes(item.optJSONArray("reason_codes")))
                    },key=="live"&&domain.optJSONArray("items")?.length()==0)
            }})
    }}
}
data class ServerSettings(val language:String,val timezone:String,val format:String,val backend:String,val autoCleanup:Boolean,
                          val acknowledgementRequired:Boolean,val terms:String) {
    companion object {fun from(value:JSONObject)=ServerSettings(value.getString("language"),value.getString("timezone"),value.getString("recording_format"),
        value.text("hardware_preferred_backend")?:"auto",value.optBoolean("auto_free_space_cleanup_enabled"),value.optBoolean("auto_free_space_acknowledgement_required"),value.optString("auto_free_space_terms_version"))}
    fun diff(baseline:ServerSettings,acknowledged:Boolean):JSONObject {
        require(language==baseline.language||language in setOf("ru","en","zh-CN"))
        require(timezone==baseline.timezone||timezone in ZoneId.getAvailableZoneIds())
        require(format==baseline.format||format in setOf("mp4","mkv"))
        require(backend==baseline.backend||backend in setOf("auto","cpu","qsv","vaapi","nvenc","amf"))
        return JSONObject().apply{
            if(language!=baseline.language)put("language",language)
            if(timezone!=baseline.timezone)put("timezone",timezone)
            if(backend!=baseline.backend)put("hardware_preferred_backend",backend)
            if(format!=baseline.format){put("recording_format",format);put("recording_format_apply_mode","after_segment");put("expected_recording_format",baseline.format)}
            if(autoCleanup!=baseline.autoCleanup){
                put("auto_free_space_cleanup_enabled",autoCleanup)
                if(autoCleanup&&baseline.acknowledgementRequired){require(acknowledged&&baseline.terms.isNotBlank());put("auto_free_space_acknowledgement",JSONObject().put("acknowledged",true).put("terms_version",baseline.terms))}
            }
        }
    }
}
class ServerApi(private val api:NasApi) {
    suspend fun storage()=StorageView.from(api.json("api/storage/status") as JSONObject)
    suspend fun system()=SystemView.from(api.json("api/system/runtime/status") as JSONObject)
    suspend fun settings()=ServerSettings.from(api.json("api/settings") as JSONObject)
    suspend fun hardware():List<String> {
        val array=(api.json("api/hardware/capabilities") as JSONObject).optJSONArray("available_backends")
        return (listOf("auto","cpu")+if(array==null)emptyList()else(0 until array.length()).map{array.optString(it)}).filter{it in setOf("auto","cpu","qsv","vaapi","nvenc","amf")}.distinct()
    }
    suspend fun save(value:ServerSettings,baseline:ServerSettings,acknowledged:Boolean)=ServerSettings.from(api.json("api/settings","PATCH",value.diff(baseline,acknowledged)) as JSONObject)
    suspend fun backups(offset:Int=0)=BackupInventory.from(api.json("api/system/restore/status?limit=20&offset=${offset.coerceAtLeast(0)}") as JSONObject)
    suspend fun createBackup(submission:String)=api.json("api/system/backup/create","POST",JSONObject().put("confirm",true).put("submission_id",validSubmission(submission))) as JSONObject
    suspend fun backupOperation(submission:String)=api.json("api/system/backup/operations/${validSubmission(submission)}") as JSONObject
    suspend fun validateBackup(artifact:String,submission:String)=api.json("api/system/restore/apply","POST",JSONObject().put("confirm",true).put("artifact_id",artifact).put("submission_id",validSubmission(submission))) as JSONObject
    suspend fun deleteBackup(artifact:String,submission:String):JSONObject {
        require(artifact.length in 1..200&&artifact !in setOf(".","..")&&artifact.none{it=='/'||it=='\\'||it.code<32})
        val encoded=URLEncoder.encode(artifact,Charsets.UTF_8.name()).replace("+","%20")
        return api.json("api/system/restore/artifacts/$encoded/delete","POST",JSONObject().put("confirm",true).put("submission_id",validSubmission(submission))) as JSONObject
    }
    private fun validSubmission(value:String)=value.also{require(it.matches(Regex("[a-f0-9-]{36}")))}
}
