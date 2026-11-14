package ru.kmvms.mobile

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.*
import ru.kmvms.mobile.ptz.PtzApi
import ru.kmvms.mobile.ptz.PtzCapabilities
import ru.kmvms.mobile.push.PushLifecycle
import ru.kmvms.mobile.push.PushUploadWorker

data class MobileState(
    val profiles: List<ServerProfile> = emptyList(),
    val active: ServerProfile? = null,
    val user: NasUser? = null,
    val cameras: List<Camera> = emptyList(),
    val connecting: Boolean = false,
    val loading: Boolean = false,
    val showConnection: Boolean = false,
    val problem: ApiProblem? = null,
    val cameraAction: Long? = null,
    val ptz: Map<Long, PtzCapabilities> = emptyMap(),
)

class MobileViewModel(application: Application) : AndroidViewModel(application) {
    private val vault = ProfileVault(application)
    private val clients = mutableMapOf<String, NasApi>()
    private var generation = 0L
    private val mutable = MutableStateFlow(MobileState())
    val state: StateFlow<MobileState> = mutable
    fun api(): NasApi? = mutable.value.active?.id?.let { clients[it] }
    init {
        PushLifecycle.opened(application)
        val profiles = vault.profiles()
        mutable.value = MobileState(profiles = profiles, showConnection = profiles.isEmpty())
        (profiles.find { it.id == vault.selectedId() } ?: profiles.firstOrNull())?.let { select(it) }
    }
    fun openConnection() { mutable.update { it.copy(showConnection = true, problem = null) } }
    fun cancelConnection() { ++generation; mutable.update { it.copy(showConnection = it.profiles.isEmpty(), connecting = false, problem = null) } }
    fun clearProblem() { mutable.update { it.copy(problem = null) } }

    fun connect(name: String, addressText: String, username: String, password: String) {
        if (mutable.value.connecting) return
        val address = try { ServerAddress.parse(addressText) } catch (_: Exception) {
            mutable.update { it.copy(problem = ApiProblem.ADDRESS) }; return
        }
        if (name.isBlank() || username.isBlank() || password.isBlank()) {
            mutable.update { it.copy(problem = ApiProblem.AUTHENTICATION) }; return
        }
        val existing = mutable.value.profiles.firstOrNull { it.address == address.toString() && it.username == username.trim() }
        val profile = ServerProfile(existing?.id ?: java.util.UUID.randomUUID().toString(), name.trim(), address.toString(), username.trim())
        val own = ++generation
        mutable.update { it.copy(connecting = true, problem = null) }
        viewModelScope.launch {
            try {
                val client = NasApi(address)
                val access = client.login(profile.username, password)
                val user = client.me()
                if (own != generation) return@launch
                vault.save(profile, password, access)
                attachRenewal(client, profile)
                clients[profile.id] = client
                mutable.value = MobileState(profiles = vault.profiles(), active = profile, user = user)
                reloadCameras()
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if (own == generation) mutable.update { it.copy(connecting = false, problem = problem(error)) }
            }
        }
    }
    fun select(profile: ServerProfile) {
        val own = ++generation
        mutable.value = MobileState(profiles = vault.profiles(), active = profile, loading = true)
        vault.select(profile.id)
        viewModelScope.launch {
            try {
                val saved = vault.signIn(profile.id) ?: throw NasException(ApiProblem.SAVED_SIGN_IN)
                val client = clients[profile.id] ?: NasApi(ServerAddress.parse(profile.address))
                client.useSession(saved.access)
                attachRenewal(client, profile)
                val user = client.me()
                if (own != generation) return@launch
                clients[profile.id] = client
                mutable.update { it.copy(user = user, loading = false) }
                reloadCameras()
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if (own == generation) mutable.update { it.copy(loading = false, problem = problem(error),
                    showConnection = problem(error) in setOf(ApiProblem.AUTHENTICATION, ApiProblem.SAVED_SIGN_IN)) }
            }
        }
    }
    fun reloadCameras() {
        val client = api() ?: return
        val user = mutable.value.user ?: return
        val own = generation
        mutable.update { it.copy(loading = true) }
        viewModelScope.launch {
            try {
                val cameras = client.cameras(user.can("manage_cameras"))
                if (own == generation) mutable.update { it.copy(cameras = cameras, loading = false) }
                if (own == generation && user.can("manage_cameras")) {
                    val concurrency = Semaphore(3)
                    cameras.filter { it.protocol == "onvif" }.forEach { camera -> launch {
                        val capability = try { concurrency.withPermit { PtzApi(client).capabilities(camera.id) } }
                        catch (cancelled: CancellationException) { throw cancelled }
                        catch (_: Exception) { PtzCapabilities.unknown() }
                        if (own == generation) mutable.update { it.copy(ptz = it.ptz + (camera.id to capability)) }
                    } }
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { if (own == generation) mutable.update { it.copy(loading = false, problem = problem(error)) } }
        }
    }
    fun setRecording(camera: Camera) {
        if (mutable.value.user?.can("manage_cameras") != true) { mutable.update { it.copy(problem = ApiProblem.FORBIDDEN) }; return }
        if (mutable.value.cameraAction != null) return
        val client = api() ?: return
        val own = generation
        mutable.update { it.copy(cameraAction = camera.id) }
        viewModelScope.launch {
            try {
                client.setRecording(camera.id, !camera.enabled)
                if (own == generation) reloadCameras()
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { if (own == generation) mutable.update { it.copy(problem = problem(error)) } }
            finally { if (own == generation) mutable.update { it.copy(cameraAction = null) } }
        }
    }
    fun signOut() {
        val profile = mutable.value.active ?: return
        val client = api()
        val disabledPush = PushLifecycle.signOut(getApplication(), profile.id)
        client?.disableSessionRenewal()
        ++generation
        vault.forgetSignIn(profile.id)
        clients.remove(profile.id)
        mutable.value = MobileState(profiles = vault.profiles(), active = profile, showConnection = true)
        viewModelScope.launch {
            try { if (client != null && disabledPush != null) PushUploadWorker.upload(client, disabledPush) } catch (_: NasException) { }
            try { client?.logout() } catch (_: NasException) { }
        }
    }
    fun removeProfile(profile: ServerProfile) {
        val active = mutable.value.active?.id == profile.id
        val client = clients.remove(profile.id)
        val disabledPush = PushLifecycle.signOut(getApplication(), profile.id)
        client?.disableSessionRenewal()
        vault.remove(profile.id)
        getApplication<Application>().getSharedPreferences("server_operations",Application.MODE_PRIVATE).edit().remove(profile.id).apply()
        if (active) {
            ++generation
            val profiles = vault.profiles()
            mutable.value = MobileState(profiles = profiles, showConnection = profiles.isEmpty())
            profiles.firstOrNull()?.let { select(it) }
        } else mutable.update { it.copy(profiles = vault.profiles()) }
        viewModelScope.launch {
            try { if (client != null && disabledPush != null) PushUploadWorker.upload(client, disabledPush) } catch (_: NasException) { }
            try { client?.logout() } catch (_: NasException) { }
        }
    }
    private fun attachRenewal(client: NasApi, profile: ServerProfile) {
        client.sessionUser(profile.username)
        client.allowSessionRenewal({ vault.signIn(profile.id) }, { expected, next -> vault.renewAccess(profile.id, expected, next) })
    }
    private fun problem(error: Exception) = (error as? NasException)?.problem ?: ApiProblem.SERVER
}
