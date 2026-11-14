package ru.kmvms.mobile.update

import android.app.Application
import android.content.Context
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import ru.kmvms.mobile.BuildConfig
import ru.kmvms.mobile.core.shouldCheckForUpdate
import java.io.File
import java.time.LocalDate

enum class UpdatePhase { IDLE, CHECKING, CURRENT, UNPUBLISHED, AVAILABLE, DOWNLOADING, READY, PERMISSION, FAILED, INCOMPATIBLE, DEVELOPMENT }
data class AppUpdateState(val phase: UpdatePhase = UpdatePhase.IDLE, val release: AndroidRelease? = null, val bytes: Long = 0,
                          val offer: Boolean = false)

/** One controller for the app, independent of the currently selected NAS. No scheduled polling. */
class AppUpdates(application: Application) : AndroidViewModel(application) {
    private val context = application.applicationContext
    private val preferences = context.getSharedPreferences("android-updates", Context.MODE_PRIVATE)
    private val source = OfficialAndroidSource()
    private val mutable = MutableStateFlow(AppUpdateState(release = cached()))
    val state = mutable.asStateFlow()
    private var running = false
    private val directory get() = File(context.cacheDir, "android-updates").apply { mkdirs() }
    private val apk get() = File(directory, "verified-update.apk")

    private fun cached(): AndroidRelease? = runCatching {
        preferences.getString("release", null)?.let { AndroidRelease.from(JSONObject(it)) }
    }.getOrNull()

    fun foreground() {
        if (mutable.value.phase == UpdatePhase.PERMISSION && context.packageManager.canRequestPackageInstalls()) {
            install()
        } else check(manual = false)
    }

    fun dismiss() { mutable.update { it.copy(offer = false) } }

    fun check(manual: Boolean) {
        if (running || mutable.value.phase in setOf(UpdatePhase.DOWNLOADING, UpdatePhase.PERMISSION)) return
        val today = LocalDate.now()
        val previous = runCatching { LocalDate.parse(preferences.getString("attempt-date", null)) }.getOrNull()
        if (!shouldCheckForUpdate(today, previous, manual)) return
        // Save BEFORE IO, including failures/offline. NAS switching never resets it.
        preferences.edit().putString("attempt-date", today.toString()).apply()
        running = true
        mutable.update { it.copy(phase = UpdatePhase.CHECKING) }
        viewModelScope.launch {
            try {
                val release = source.latest()
                preferences.edit().putString("release", release?.json()?.toString()).apply()
                val phase = when {
                    release == null -> UpdatePhase.UNPUBLISHED
                    release.minSdk > Build.VERSION.SDK_INT -> UpdatePhase.INCOMPATIBLE
                    release.code <= BuildConfig.VERSION_CODE -> UpdatePhase.CURRENT
                    BuildConfig.DEBUG -> UpdatePhase.DEVELOPMENT
                    else -> UpdatePhase.AVAILABLE
                }
                mutable.value = AppUpdateState(phase, release, offer = phase == UpdatePhase.AVAILABLE)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { mutable.update { it.copy(phase = UpdatePhase.FAILED) } }
            finally { running = false }
        }
    }

    fun download() {
        if (running || mutable.value.phase !in setOf(UpdatePhase.AVAILABLE, UpdatePhase.FAILED)) return
        val release = mutable.value.release ?: return
        if (BuildConfig.DEBUG || release.code <= BuildConfig.VERSION_CODE) return
        running = true
        mutable.update { it.copy(phase = UpdatePhase.DOWNLOADING, bytes = 0, offer = false) }
        viewModelScope.launch {
            try {
                source.download(release, apk) { bytes -> mutable.update { it.copy(bytes = bytes) } }
                withContext(Dispatchers.IO) { verifyApk(context, apk, release) }
                mutable.update { it.copy(phase = UpdatePhase.READY) }
                install()
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { apk.delete(); mutable.update { it.copy(phase = UpdatePhase.FAILED) } }
            finally { running = false }
        }
    }

    fun install() {
        val release = mutable.value.release ?: return
        if (mutable.value.phase !in setOf(UpdatePhase.READY, UpdatePhase.PERMISSION)) return
        viewModelScope.launch {
            try {
                withContext(Dispatchers.IO) { verifyApk(context, apk, release) }
                val launched = launchApkInstaller(context, apk)
                mutable.update { it.copy(phase = if (launched) UpdatePhase.READY else UpdatePhase.PERMISSION) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { apk.delete(); mutable.update { it.copy(phase = UpdatePhase.FAILED) } }
        }
    }
}
