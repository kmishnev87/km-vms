package ru.kmvms.mobile.camera

import org.json.JSONObject
import ru.kmvms.mobile.data.NasApi

data class CameraCatalog(val profiles:List<CameraProfile>,val sources:List<CameraSource>,val proof:String,val main:String?=null,val sub:String?=null) {
    override fun toString()="CameraCatalog(profiles=${profiles.size}, sources=${sources.size}, proof=[private])"
    companion object { fun from(value:JSONObject):CameraCatalog {
        val profiles=value.getJSONArray("profiles");val sources=value.optJSONArray("source_groups")
        return CameraCatalog((0 until profiles.length()).map{CameraProfile.from(profiles.getJSONObject(it))},
            if(sources==null)emptyList()else(0 until sources.length()).map{CameraSource.from(sources.getJSONObject(it))},value.getString("onvif_probe_token"),
            value.text("suggested_main_profile_token"),value.text("suggested_sub_profile_token"))
    } }
}
class CameraApi(private val api:NasApi) {
    suspend fun get(id:Long)=api.json("api/cameras/${validId(id)}") as JSONObject
    suspend fun probe(draft:CameraDraft,id:Long?)=CameraCatalog.from(api.json("api/cameras/onvif/probe","POST",draft.payload(id).put("timeout_seconds",8)) as JSONObject)
    suspend fun test(draft:CameraDraft,id:Long?,role:String):JSONObject {
        require(role in setOf("main","sub"))
        return api.json("api/cameras/test","POST",draft.payload(id).put("stream_role",role).put("include_preview",role=="main")) as JSONObject
    }
    suspend fun metadata(draft:CameraDraft,id:Long?)=api.json("api/cameras/test","POST",draft.payload(id).put("stream_role","main").put("metadata_only",true).put("include_preview",false)) as JSONObject
    suspend fun events(draft:CameraDraft,id:Long?)=(api.json("api/cameras/onvif/events/capabilities","POST",draft.payload(id)) as JSONObject).optBoolean("supported")
    suspend fun configuration(draft:CameraDraft,id:Long?,token:String)=api.json("api/cameras/onvif/profile_config","POST",draft.payload(id).put("profile_token",token)) as JSONObject
    suspend fun updateConfiguration(draft:CameraDraft,id:Long?,token:String,diff:JSONObject)=api.json("api/cameras/onvif/update_profile","POST",draft.payload(id).put("profile_token",token).put("config",diff)) as JSONObject
    suspend fun save(id:Long?,payload:JSONObject):JSONObject {
        payload.remove("camera_id")
        return api.json(if(id==null)"api/cameras" else "api/cameras/${validId(id)}",if(id==null)"POST" else "PUT",payload) as JSONObject
    }
    suspend fun deletePreview(id:Long,deleteFiles:Boolean)=api.json("api/cameras/${validId(id)}/delete-preview?delete_files=$deleteFiles") as JSONObject
    suspend fun delete(id:Long,deleteFiles:Boolean,operationId:String):JSONObject {
        require(operationId.matches(Regex("[a-f0-9-]{36}")))
        return api.json("api/cameras/${validId(id)}?delete_files=$deleteFiles&operation_id=$operationId","DELETE") as JSONObject
    }
    private fun validId(id:Long)=id.also{require(it>0)}
}
