package ru.kmvms.mobile.diagnostics

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import ru.kmvms.mobile.BuildConfig

enum class ApkReportPhase { IDLE, PREPARING, SENDING, ACCEPTED, FAILED }
data class ApkReportState(val phase: ApkReportPhase = ApkReportPhase.IDLE, val bytes: Long = 0, val total: Long = 0,
    val receipt: String? = null, val errorStatus: Int = 0)

class ApkReportController(application: Application) : AndroidViewModel(application) {
    private val mutable = MutableStateFlow(ApkReportState())
    val state = mutable.asStateFlow()
    private var attempt: ApkReportAttempt? = null
    fun reset() { if (mutable.value.phase !in setOf(ApkReportPhase.PREPARING, ApkReportPhase.SENDING)) { attempt = null; mutable.value = ApkReportState() } }
    fun send(comment: String) {
        if (comment.length > 4000 || mutable.value.phase in setOf(ApkReportPhase.PREPARING, ApkReportPhase.SENDING, ApkReportPhase.ACCEPTED)) return
        mutable.value = ApkReportState(ApkReportPhase.PREPARING)
        viewModelScope.launch {
            try {
                val pending = attempt?.takeIf { it.comment == comment } ?: withContext(Dispatchers.IO) {
                    ApkReportAttempt.create(BuildConfig.VERSION_NAME, comment, AppDiagnostics.snapshot(getApplication()))
                }.also { attempt = it }
                mutable.value = ApkReportState(ApkReportPhase.SENDING, total = pending.bytes.size.toLong())
                val receipt = ApkReportDelivery().send(pending) { bytes, total -> mutable.update { it.copy(bytes = bytes, total = total) } }
                AppDiagnostics.record(DiagnosticCode.REPORT_ACCEPTED, DiagnosticArea.SUPPORT)
                mutable.update { it.copy(phase = ApkReportPhase.ACCEPTED, receipt = receipt) }
                attempt = null
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                val status = (error as? ApkReportException)?.status ?: 0
                AppDiagnostics.record(DiagnosticCode.REPORT_FAILED, DiagnosticArea.SUPPORT, number = status)
                mutable.update { it.copy(phase = ApkReportPhase.FAILED, errorStatus = status) }
            }
        }
    }
}
