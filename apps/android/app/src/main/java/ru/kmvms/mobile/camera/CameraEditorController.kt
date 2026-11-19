package ru.kmvms.mobile.camera

import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import org.json.JSONObject

data class CameraEditorState(val draft:CameraDraft=CameraDraft.empty(),val phase:Int=0,val busy:Boolean=false,
    val catalog:CameraCatalog?=null,val source:String?=null,val eventsSupported:Boolean?=null,
    val configurations:Map<String,JSONObject> = emptyMap(),val verified:Boolean=false,val saved:Boolean=false,val failures:Int=0,
    val eventsWereEnabled:Boolean=false)

class CameraEditorController(private val api:CameraApi,private val id:Long?) {
    private val scope=CoroutineScope(SupervisorJob()+Dispatchers.Main.immediate)
    private val mutable=MutableStateFlow(CameraEditorState(busy=id!=null))
    val state=mutable.asStateFlow()
    private var baseline=CameraDraft.empty()
    private var generation=0
    private var request:Job?=null
    private val proofs=mutableMapOf<String,String>()
    private var proofAt=0L
    init { if(id!=null) request=scope.launch {
        try { baseline=CameraDraft.from(api.get(id));mutable.update{it.copy(draft=baseline,eventsWereEnabled=baseline.flag("events_enabled"))} }
        catch(cancelled:CancellationException){throw cancelled}
        catch(_:Exception){error()}
        finally{mutable.update{it.copy(busy=false)}}
    } }
    fun change(key:String,value:String) {
        if(mutable.value.busy)return
        val draft=mutable.value.draft.change(key,value)
        if(key in CameraDraft.connectionFields){generation++;proofs.clear();proofAt=0L}
        val management=key in setOf("protocol","host","port","username","password","rtsp_host","rtsp_port","onvif_path")
        mutable.update{it.copy(draft=draft,verified=if(key in CameraDraft.connectionFields)false else it.verified,
            catalog=if(management)null else it.catalog,configurations=if(management)emptyMap()else it.configurations)}
    }
    fun source(source:CameraSource) {
        if(mutable.value.busy)return
        val catalog=mutable.value.catalog?:return
        generation++;proofs.clear();proofAt=0L
        mutable.update{it.copy(draft=forSource(it.draft,catalog,source),source=source.id,verified=false,configurations=emptyMap())}
        check(probe=false)
    }
    fun profile(role:String,profile:CameraProfile) {
        if(mutable.value.busy||role !in setOf("main","sub"))return
        val state=mutable.value
        val group=state.catalog?.sources?.find{it.id==state.source}
        require(group==null||profile.token in group.profiles)
        generation++;proofs.clear();proofAt=0L
        mutable.update{it.copy(draft=assign(it.draft,role,profile),verified=false)}
        check(probe=false)
    }
    fun purpose(purpose:String,profile:CameraProfile) {
        if(mutable.value.busy||purpose !in setOf("record","live"))return
        var draft=mutable.value.draft
        val group=mutable.value.catalog?.sources?.find{it.id==mutable.value.source}
        require(group==null||profile.token in group.profiles)
        val field="default_${purpose}_stream"
        val other="default_${if(purpose=="record")"live"else"record"}_stream"
        val existing=draft.roles().find{draft[if(it=="main")"onvif_profile_token"else"onvif_sub_profile_token"]==profile.token}
        val role=existing?:if(draft[field]!=draft[other])draft[field]else if(draft[other]=="main")"sub"else"main"
        draft=assign(draft,role,profile).change(field,role)
        listOf("main","sub").filter{it!=draft[field]&&it!=draft[other]}.forEach{draft=assign(draft,it,null)}
        generation++;proofs.clear();proofAt=0L
        mutable.update{it.copy(draft=draft,verified=false)}
        check(probe=false)
    }
    fun phase(phase:Int){if(!mutable.value.busy)mutable.update{it.copy(phase=phase.coerceIn(0,2))}}
    fun next() {
        if(mutable.value.phase==0)check(next=true)
        else phase(mutable.value.phase+1)
    }
    fun check(probe:Boolean=true,next:Boolean=false)=operation { own ->
        verify(own,probe)
        if(next&&own==generation)mutable.update{it.copy(phase=1)}
    }
    private suspend fun verify(own:Int,probe:Boolean) {
        var draft=mutable.value.draft
        require(draft.validConnection(id!=null)&&draft.validPolicy())
        var catalog=mutable.value.catalog
        if(draft["protocol"]=="onvif"&&(probe||catalog==null)) {
            catalog=api.probe(draft,id);ensureCurrent(own)
            val selected=catalog.profiles.find{it.token==draft["onvif_profile_token"]}
                ?:catalog.profiles.singleOrNull{it.path==draft["rtsp_main_url"]}
            val source=catalog.sources.find{selected?.token in it.profiles}?:if(id==null)catalog.sources.firstOrNull()else null
            if(source!=null&&selected==null)draft=forSource(draft,catalog,source)
            else if(selected!=null)draft=assign(draft,"main",selected)
            else if(id==null) {
                val main=catalog.profiles.find{it.token==catalog.main}?:catalog.profiles.firstOrNull{it.path.isNotBlank()}
                draft=assign(draft,"main",main)
                draft=assign(draft,"sub",catalog.profiles.find{it.token==catalog.sub&&it.token!=main?.token})
            }
            require(draft.roles().isNotEmpty())
            proofs["onvif_probe_token"]=catalog.proof
            mutable.update{it.copy(draft=draft,catalog=catalog,source=source?.id,configurations=emptyMap())}
            val supports=try{api.events(draft,id)}catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){null}
            ensureCurrent(own);mutable.update{it.copy(eventsSupported=supports)}
        }
        require(draft.roles().isNotEmpty())
        if(draft["protocol"]=="onvif")proofs["onvif_probe_token"]=(catalog?:throw IllegalStateException("Read ONVIF profiles first")).proof
        for(field in listOf("default_record_stream","default_live_stream"))if(draft[field] !in draft.roles())draft=draft.change(field,draft.roles().first())
        mutable.update{it.copy(draft=draft)}
        val used=catalog?.profiles.orEmpty().filter{it.token in listOf(draft["onvif_profile_token"],draft["onvif_sub_profile_token"])}
        require(used.mapNotNull{it.source}.distinct().size<=1)
        for(role in draft.roles()) {
            val result=api.test(draft,id,role);ensureCurrent(own)
            require(result.optBoolean("ok")&&result.optJSONObject("video")!=null&&result.optString("tested_role")==role)
            proofs["${role}_validation_token"]=result.getString("validation_token")
            if(role=="main") {
                proofs["validation_token"]=result.getString("validation_token")
                result.text("preview_token")?.takeIf { result.optBoolean("preview_ok") }?.let { proofs["preview_token"]=it }
            }
        }
        val currentCatalog=catalog
        if(currentCatalog!=null) {
            val source=mutable.value.source?.let{target->currentCatalog.sources.find{it.id==target}}
            val profiles=currentCatalog.profiles.filter{source==null||it.token in source.profiles}
            val concurrency=Semaphore(3)
            val configurations=coroutineScope{profiles.map { profile->async {
                concurrency.withPermit {
                    var result=try{api.configuration(draft,id,profile.token)}catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){null}
                    val config=result?.optJSONObject("config")
                    if((config?.text("codec")?:profile.codec)==null||(config?.text("fps")?:profile.fps)==null||
                        (config?.text("resolution")?:profile.resolution)==null) {
                        val video=try{api.metadata(assign(draft,"main",profile),id).optJSONObject("video")}
                            catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){null}
                        if(video!=null)result=(result?:JSONObject()).put("_native_probe_video",video)
                    }
                    profile.token to result
                }
            }}.awaitAll().filter{it.second!=null}.associate{it.first to it.second!!}}
            ensureCurrent(own)
            val enriched=currentCatalog.copy(profiles=currentCatalog.profiles.map { profile ->
                val current=configurations[profile.token];val config=current?.optJSONObject("config");val video=current?.optJSONObject("_native_probe_video")
                profile.copy(codec=config?.text("codec")?:video?.text("codec")?:profile.codec,
                    resolution=config?.text("resolution")?:if((video?.optInt("width")?:0)>0)"${video?.optInt("width")}×${video?.optInt("height")}"else profile.resolution,
                    fps=config?.text("fps")?:video?.text("fps")?:profile.fps)
            })
            mutable.update{it.copy(catalog=enriched,configurations=configurations)}
        }
        ensureCurrent(own);proofAt=System.nanoTime();mutable.update{it.copy(verified=true)}
    }
    fun save()=operation { own ->
        val draft=mutable.value.draft
        require(draft.validPolicy())
        val connectionChanged=id==null||draft.connectionChanged(baseline)
        if(connectionChanged&&(!mutable.value.verified||(System.nanoTime()-proofAt)>12L*60*1_000_000_000))verify(own,probe=true)
        ensureCurrent(own)
        val current=mutable.value.draft
        val payload=current.payload(baseline=if(id==null)null else baseline)
        proofs.forEach{(key,value)->payload.put(key,value)}
        api.save(id,payload);ensureCurrent(own);mutable.update{it.copy(saved=true)}
    }
    fun applyConfiguration(token:String,diff:JSONObject)=operation { own ->
        require(diff.length()>0&&mutable.value.catalog?.profiles?.any{it.token==token}==true)
        val configuration=mutable.value.configurations[token]?:throw IllegalStateException("Read settings first")
        val check=EncoderDraft.from(configuration)
        var changed=check
        diff.keys().forEach{key->changed=changed.change(key,diff.get(key).toString())}
        require(changed.valid()&&changed.diff().length()>0)
        proofs.clear();proofAt=0L;mutable.update{it.copy(verified=false)}
        try {
            val result=api.updateConfiguration(mutable.value.draft,id,token,changed.diff());ensureCurrent(own)
            require(result.optBoolean("ok")&&(!result.optBoolean("changed")||result.optJSONObject("verification")?.optBoolean("matched")==true))
            result.text("stream_path")?.let{path->
                var draft=mutable.value.draft
                for(role in draft.roles())if(draft[if(role=="main")"onvif_profile_token"else"onvif_sub_profile_token"]==token)draft=draft.change("rtsp_${role}_url",path)
                mutable.update{it.copy(draft=draft)}
            }
            mutable.update{it.copy(configurations=it.configurations+(token to result))}
            verify(own,probe=false)
        } catch(cancelled:CancellationException){throw cancelled}
        catch(error:Exception) {
            // A timeout is not proof of refusal: drop stale values and require a fresh read.
            mutable.update{it.copy(configurations=it.configurations-token)}
            throw error
        }
    }
    private fun operation(block:suspend(Int)->Unit) {
        if(mutable.value.busy)return
        val own=generation
        mutable.update{it.copy(busy=true)}
        request=scope.launch{try{block(own)}catch(cancelled:CancellationException){throw cancelled}catch(_:Exception){if(own==generation)error()}finally{if(own==generation)mutable.update{it.copy(busy=false)}}}
    }
    private fun ensureCurrent(own:Int){if(own!=generation)throw CancellationException("Camera form changed")}
    private fun error(){mutable.update{it.copy(failures=it.failures+1)}}
    fun close(){generation++;scope.cancel();proofs.clear()}
    internal suspend fun awaitClosed() { scope.coroutineContext[Job]?.join() }
    companion object {
        fun assign(draft:CameraDraft,role:String,profile:CameraProfile?):CameraDraft = draft
            .change(if(role=="main")"onvif_profile_token"else"onvif_sub_profile_token",profile?.token.orEmpty())
            .change("rtsp_${role}_url",profile?.path.orEmpty())
        fun forSource(draft:CameraDraft,catalog:CameraCatalog,source:CameraSource):CameraDraft {
            var result=assign(draft,"main",catalog.profiles.find{it.token==source.main})
            result=assign(result,"sub",catalog.profiles.find{it.token==source.sub})
            return result.change("onvif_channel_id",source.channel.orEmpty()).change("default_record_stream","main")
                .change("default_live_stream",if(source.sub!=null)"sub"else"main")
        }
    }
}
