package ru.kmvms.mobile.chronology

import android.content.Context
import androidx.media3.common.MediaItem
import androidx.media3.common.C
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.SeekParameters
import androidx.media3.exoplayer.source.ProgressiveMediaSource
import androidx.media3.exoplayer.source.ClippingMediaSource
import androidx.media3.exoplayer.source.MediaSource
import ru.kmvms.mobile.core.TimelinePhase
import ru.kmvms.mobile.core.TimelineSample
import kotlin.math.abs
import ru.kmvms.mobile.diagnostics.*

@UnstableApi
class TimelineTrack(context: Context, private val api: ChronologyApi, val cameraId: Long) {
    private data class Slot(val source: TimelineSource, val clipOffset: Long)
    private val slots = linkedMapOf<String, Slot>()
    private var state = TimelinePhase.RESOLVING
    private var unsupported = false
    private var suspended = false
    private val loadControl=ArchiveLoadControl()
    val player = ExoPlayer.Builder(context.applicationContext).setLoadControl(loadControl).build().apply {
        volume = 0f
        setSeekParameters(SeekParameters.EXACT)
        addListener(object : Player.Listener {
            override fun onPlaybackStateChanged(playbackState: Int) {
                if (state in setOf(TimelinePhase.EMPTY, TimelinePhase.FAILED)) return
                state = if (playbackState == Player.STATE_READY) TimelinePhase.READY else TimelinePhase.BUFFERING
            }
            override fun onPlayerError(error: PlaybackException) {
                AppDiagnostics.record(DiagnosticCode.ARCHIVE_FAILED, DiagnosticArea.ARCHIVE, camera = cameraId, number = error.errorCode)
                state = TimelinePhase.FAILED
                unsupported = error.errorCode in setOf(PlaybackException.ERROR_CODE_DECODER_INIT_FAILED,
                    PlaybackException.ERROR_CODE_DECODING_FORMAT_UNSUPPORTED,
                    PlaybackException.ERROR_CODE_DECODING_FORMAT_EXCEEDS_CAPABILITIES)
            }
        })
    }

    private fun slot() = player.currentMediaItem?.mediaId?.let(slots::get)
    fun source(): TimelineSource? = slot()?.source
    fun phase(): TimelinePhase = state
    fun unsupportedCodec() = unsupported
    fun ended() = player.playbackState == Player.STATE_ENDED && source() != null
    fun canServe(position: Long) = source()?.let { position >= it.start - 500 && position < it.end } == true
    fun hasQueuedNext() = player.hasNextMediaItem()
    fun bufferBudget(bytes:Int){loadControl.budgetBytes=bytes}

    fun sample(): TimelineSample {
        val slot = slot()
        val position = slot?.let { it.source.start + it.clipOffset + player.currentPosition.coerceAtLeast(0) }
        val remaining=if(position!=null && slot!=null)(slot.source.end-position).coerceAtLeast(0) else Long.MAX_VALUE
        return TimelineSample(cameraId, state, position,player.totalBufferedDuration.coerceAtLeast(0),remaining,loadControl.capacityMillis)
    }

    fun resolving() { state = TimelinePhase.RESOLVING; player.pause() }
    fun empty() { player.stop(); state = TimelinePhase.EMPTY }
    fun failed() { player.pause(); state = TimelinePhase.FAILED }

    fun load(source: TimelineSource, target: Long) {
        require(source.cameraId == cameraId)
        unsupported = false
        if (source().let { it?.key == source.key }) {
            state = TimelinePhase.BUFFERING
            seek(target)
            if (player.playbackState in setOf(Player.STATE_IDLE, Player.STATE_ENDED)) player.prepare()
        } else {
            player.pause()
            slots.clear()
            slots[source.key] = Slot(source, 0)
            state = TimelinePhase.BUFFERING
            player.setMediaSource(mediaSource(source, 0))
            player.seekTo((target - source.start).coerceAtLeast(0))
            player.prepare()
        }
    }

    fun append(source: TimelineSource, boundary: Long) {
        if (source.key in slots || hasQueuedNext()) return
        val clip = (boundary - source.start).coerceAtLeast(0)
        if (source.end <= boundary || source.start > boundary + 50) return
        slots[source.key] = Slot(source, clip)
        player.addMediaSource(mediaSource(source, clip))
    }

    private fun mediaSource(source: TimelineSource, clip: Long): MediaSource {
        val item = MediaItem.Builder().setMediaId(source.key)
            .setUri(api.api.address.endpoint("api/chronology/file?${source.query()}").toString())
            .setClippingConfiguration(MediaItem.ClippingConfiguration.Builder().setStartPositionMs(clip).build()).build()
        val progressive = ProgressiveMediaSource.Factory(ChronologyDataSource.Factory(api, source)).createMediaSource(item)
        return if (clip > 0) ClippingMediaSource.Builder(progressive).setStartPositionMs(clip).build() else progressive
    }

    fun seek(position: Long) {
        val slot = slot() ?: return
        if (!canServe(position)) return
        player.pause()
        val offset=(position-slot.source.start-slot.clipOffset).coerceAtLeast(0)
        // A common buffer hold is not a new source/seek. Keep healthy buffers.
        if(abs(player.currentPosition-offset)>50)player.seekTo(offset)
        if (player.playbackState == Player.STATE_READY) state = TimelinePhase.READY
    }

    fun playback(run: Boolean, speed: Float) {
        if (player.playbackParameters.speed != speed) player.setPlaybackSpeed(speed)
        player.playWhenReady = run && state == TimelinePhase.READY && !suspended
        // Retain one item behind the active item at most. Removing an old playlist
        // entry does not replace the current decoder/source or flash the picture.
        if (player.currentMediaItemIndex > 1) {
            val key = player.getMediaItemAt(0).mediaId
            player.removeMediaItem(0)
            slots.remove(key)
        }
    }

    fun foreground(active: Boolean) {
        suspended = !active
        if (!active) { player.pause(); player.stop() }
        else if (state !in setOf(TimelinePhase.EMPTY, TimelinePhase.FAILED, TimelinePhase.RESOLVING)) {
            state = TimelinePhase.BUFFERING
            player.prepare()
        }
    }

    fun close() = player.release()
}
