package ru.kmvms.mobile.camera

import org.json.JSONObject

/** A short-lived form. Credentials are never saved with UI state or rendered by toString. */
class CameraDraft private constructor(private val fields: Map<String,String>) {
    operator fun get(key: String): String = fields[key].orEmpty()
    fun flag(key: String) = this[key] == "true"
    fun change(key: String, value: String): CameraDraft {
        require(key in defaults)
        val next=fields.toMutableMap().apply{put(key,value)}
        if(key=="host" && this["protocol"]=="onvif" && this["rtsp_host"] in listOf("",this["host"]))next["rtsp_host"]=value
        if(key=="protocol") {
            next["port"]=if(value=="onvif")"80" else "554"
            next["events_enabled"]="false"
            listOf("onvif_path","onvif_profile_token","onvif_sub_profile_token","onvif_channel_id","rtsp_main_url","rtsp_sub_url").forEach{next[it]=""}
        }
        return CameraDraft(next)
    }
    fun changedFrom(other: CameraDraft): Set<String> = defaults.keys.filter { key ->
        !(key=="password"&&this[key].isEmpty()) && this[key]!=other[key]
    }.toSet()
    fun connectionChanged(other: CameraDraft)=changedFrom(other).any{it in connectionFields}
    fun validConnection(existing: Boolean): Boolean = this["host"].isNotBlank() && this["port"].toIntOrNull() in 1..65535 &&
        (this["protocol"]!="onvif" || this["rtsp_port"].toIntOrNull() in 1..65535 && this["username"].isNotBlank() && (existing||this["password"].isNotBlank()))
    fun validPolicy()=this["name"].isNotBlank()&&this["name"].codePointCount(0,this["name"].length)<=255&&
        this["segment_minutes"].toIntOrNull()?.let{it>0}==true&&this["retention_days"].toIntOrNull() in 1..36500&&
        this["storage_quota_gb"].toIntOrNull()?.let{it>0}==true
    fun roles()=listOf("main","sub").filter{this["rtsp_${it}_url"].isNotBlank()}
    fun payload(id: Long? = null, baseline: CameraDraft? = null): JSONObject = JSONObject().apply {
        val keys=baseline?.let{changedFrom(it)}?:defaults.keys
        keys.forEach { key ->
            if(key=="password"&&this@CameraDraft[key].isEmpty())return@forEach
            if(this@CameraDraft["protocol"]!="onvif"&&key in setOf("rtsp_host","rtsp_port"))return@forEach
            put(key,when(key){in integerFields->this@CameraDraft[key].toInt();in booleanFields->flag(key);else->this@CameraDraft[key]})
        }
        id?.let{put("camera_id",it)}
    }
    override fun toString()="CameraDraft([private form])"
    companion object {
        val connectionFields=setOf("protocol","host","port","username","password","rtsp_host","rtsp_port","rtsp_transport","rtsp_main_url","rtsp_sub_url","onvif_path","onvif_profile_token","onvif_sub_profile_token","onvif_channel_id")
        private val integerFields=setOf("port","rtsp_port","segment_minutes","retention_days","storage_quota_gb")
        private val booleanFields=setOf("enabled","events_enabled")
        private val defaults=mapOf("name" to "","enabled" to "true","events_enabled" to "false","protocol" to "onvif",
            "host" to "","port" to "80","username" to "","password" to "","rtsp_main_url" to "","rtsp_sub_url" to "",
            "rtsp_host" to "","rtsp_port" to "554","rtsp_transport" to "tcp","onvif_path" to "","onvif_profile_token" to "",
            "onvif_sub_profile_token" to "","onvif_channel_id" to "","recording_mode" to "always","default_live_stream" to "sub",
            "default_record_stream" to "main","segment_minutes" to "5","retention_days" to "30","storage_quota_gb" to "50")
        fun empty()=CameraDraft(defaults)
        fun from(value: JSONObject)=CameraDraft(defaults.mapValues { (key,fallback)->if(key=="password")"" else value.opt(key)?.takeUnless{it==JSONObject.NULL}?.toString()?:fallback }.toMutableMap().apply{
            if(value.optString("protocol")=="onvif") {
                put("rtsp_host",value.optString("rtsp_reachable_host").takeUnless{it.isBlank()||it=="null"}?:value.optString("rtsp_host").takeUnless{it.isBlank()||it=="null"}?:value.optString("host"))
                put("rtsp_port",value.optInt("rtsp_reachable_port",value.optInt("rtsp_port",554)).takeIf{it>0}?.toString()?:"554")
            }
        })
    }
}

data class CameraProfile(val token: String,val name: String,val path: String,val source: String?,val channel: String?,
                         val codec: String?,val resolution: String?,val fps: String?) {
    fun description()=listOfNotNull(codec,resolution,fps?.let{"$it fps"}).joinToString(" · ")
    companion object {
        fun from(value: JSONObject): CameraProfile {
            val video=value.optJSONObject("rtsp_probe")?.optJSONObject("video")?:value.optJSONObject("video")
            val current=value.optJSONObject("current")?:value.optJSONObject("settings")
            return CameraProfile(value.getString("token"),value.optString("name"),value.optString("stream_path"),value.text("source_id"),value.text("source_channel"),
                current?.text("codec")?:video?.text("codec"),current?.text("resolution")?:if((video?.optInt("width")?:0)>0)"${video?.optInt("width")}×${video?.optInt("height")}" else null,
                current?.text("fps")?:video?.text("fps"))
        }
    }
}
data class CameraSource(val id:String,val label:String,val channel:String?,val profiles:Set<String>,val main:String?,val sub:String?) {
    companion object { fun from(value:JSONObject):CameraSource {
        val tokens=value.getJSONArray("profile_tokens")
        return CameraSource(value.getString("id"),value.text("label")?:value.text("name")?:value.getString("id"),value.text("channel"),
            (0 until tokens.length()).map{tokens.getString(it)}.toSet(),value.text("main_profile_token"),value.text("sub_profile_token"))
    } }
}
internal fun JSONObject.text(key:String)=optString(key).takeUnless{it.isBlank()||it=="null"}
