package ru.kmvms.mobile.talk

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/** Native audio I/O never runs on Compose's main thread. Nothing is written to disk. */
internal class NativeMicrophone {
    private val stopped = AtomicBoolean()
    private val recorder = AtomicReference<AudioRecord?>()
    private val echo = AtomicReference<AcousticEchoCanceler?>()

    @SuppressLint("MissingPermission")
    fun open(): AudioRecord {
        val minimum = AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        require(minimum > 0)
        val own = AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, 16000,
            AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(minimum, 640 * 4))
        recorder.set(own)
        try {
            check(!stopped.get() && own.state == AudioRecord.STATE_INITIALIZED)
            if (AcousticEchoCanceler.isAvailable()) echo.set(AcousticEchoCanceler.create(own.audioSessionId)?.apply { enabled = true })
            check(!stopped.get())
            own.startRecording()
            check(!stopped.get() && own.recordingState == AudioRecord.RECORDSTATE_RECORDING)
            return own
        } catch (error: Exception) { close(); throw error }
    }

    fun stopSending() { stopped.set(true) }
    fun close() {
        stopped.set(true)
        recorder.getAndSet(null)?.let { own -> runCatching { own.stop() }; runCatching { own.release() } }
        runCatching { echo.getAndSet(null)?.release() }
    }
}
