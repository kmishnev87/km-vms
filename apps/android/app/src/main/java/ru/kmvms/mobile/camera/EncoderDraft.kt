package ru.kmvms.mobile.camera

import org.json.JSONObject
import kotlin.math.abs

/** Only fields actually declared writable by this camera/Media service can enter a Set request. */
class EncoderDraft(val contract:JSONObject,private val values:Map<String,String>) {
    operator fun get(key:String)=values[key].orEmpty()
    fun meta(key:String):JSONObject = fields().optJSONObject(key)?:JSONObject()
    fun options(key:String):List<String> {
        val array=meta(key).optJSONArray("options")?:return emptyList()
        return (0 until array.length()).map{array.get(it).toString()}
    }
    private fun fields():JSONObject {
        val group=contract.optJSONObject("options_by_codec")?.optJSONObject(this["codec"])
        return group?.optJSONObject("by_resolution")?.optJSONObject(this["resolution"])
            ?:group?.optJSONObject("fields")?:contract.optJSONObject("supported")?:JSONObject()
    }
    fun change(key:String,value:String):EncoderDraft {
        require(key in names)
        var next=EncoderDraft(contract,values+(key to value))
        if(key in setOf("codec","resolution")) {
            val resolutions=next.options("resolution")
            if(resolutions.isNotEmpty()&&next["resolution"] !in resolutions)next=EncoderDraft(contract,next.values+("resolution" to resolutions.first()))
            for(name in names-setOf("codec","resolution")) {
                if(!next.meta(name).optBoolean("writable")||next[name].isEmpty())continue
                val options=next.options(name)
                var current=next[name]
                if(options.isNotEmpty()&&options.none{equal(name,it,current)})current=if(name in numeric)options.minByOrNull{abs((it.toDoubleOrNull()?:Double.MAX_VALUE)-(current.toDoubleOrNull()?:0.0))}!! else options.first()
                val range=next.meta(name).optJSONObject("range")
                if(name in numeric&&range!=null)current=current.toDoubleOrNull()?.let{number->
                    number.coerceIn(range.optDouble("min",-Double.MAX_VALUE),range.optDouble("max",Double.MAX_VALUE)).let{if(name in integers||next.meta(name).optString("number_type")=="integer")it.toLong().toString()else it.toString()}
                }?:current
                next=EncoderDraft(contract,next.values+(name to current))
            }
        }
        return next
    }
    fun valid():Boolean=names.all{key->
        if(!meta(key).optBoolean("writable")||equal(key,this[key],original(key)))true
        else {
            val value=this[key];val options=options(key);val number=value.toDoubleOrNull();val range=meta(key).optJSONObject("range")
            value.isNotBlank()&&(options.isEmpty()||options.any{equal(key,it,value)})&&
                (key !in numeric||number!=null&&number.isFinite()&&
                    (key !in integers&&meta(key).optString("number_type")!="integer"||number%1.0==0.0)&&
                    (range==null||number>=range.optDouble("min",-Double.MAX_VALUE)&&number<=range.optDouble("max",Double.MAX_VALUE)))
        }
    }
    fun diff():JSONObject=JSONObject().apply{names.forEach{key->
        val value=this@EncoderDraft[key]
        if(meta(key).optBoolean("writable")&&value.isNotBlank()&&!equal(key,value,original(key)))put(key,if(key in numeric)value.toDouble()else value)
    }}
    private fun original(key:String):String {
        val config=contract.optJSONObject("config")?:return ""
        return config.text(key)?:if(key=="resolution"&&config.optInt("width")>0)"${config.optInt("width")}x${config.optInt("height")}"else""
    }
    companion object {
        val names=listOf("codec","codec_profile","resolution","fps","bitrate_mode","bitrate","iframe_interval","quality")
        private val numeric=setOf("fps","bitrate","iframe_interval","quality")
        private val integers=setOf("bitrate","iframe_interval")
        fun from(value:JSONObject):EncoderDraft {
            val config=value.optJSONObject("config")?:JSONObject()
            return EncoderDraft(value,names.associateWith{key->config.text(key)?:if(key=="resolution"&&config.optInt("width")>0)"${config.optInt("width")}x${config.optInt("height")}"else""})
        }
        private fun equal(key:String,first:String,second:String)=if(key in numeric&&first.isNotBlank()&&second.isNotBlank()) {
            first.toDoubleOrNull()?.let{left->second.toDoubleOrNull()?.let{right->abs(left-right)<1e-9}}?:false
        }else first.trim().equals(second.trim(),ignoreCase=true)
    }
}
