package ru.kmvms.mobile.archive

import android.content.Context
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.ProgressiveMediaSource
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.media.RecordingDataSource
import ru.kmvms.mobile.media.VideoState
import ru.kmvms.mobile.media.VideoStatus

data class ArchivePosition(val position: Long = 0, val duration: Long = 0, val playing: Boolean = false)

@UnstableApi
class ArchivePlaybackSession(context: Context, api: NasApi, recording: Recording, initialPosition: Long) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val mutableVideo = MutableStateFlow(VideoState())
    private val mutablePosition = MutableStateFlow(ArchivePosition(initialPosition))
    val video = mutableVideo.asStateFlow()
    val position = mutablePosition.asStateFlow()
    private var foreground = true
    private var intentToPlay = true
    val player = ExoPlayer.Builder(context.applicationContext).build().apply {
        setMediaSource(ProgressiveMediaSource.Factory(RecordingDataSource.Factory(api, recording))
            .createMediaSource(MediaItem.fromUri(api.address.endpoint("api/recordings/stream?${recording.query()}").toString())))
        seekTo(initialPosition.coerceAtLeast(0)); playWhenReady = true
        addListener(object : Player.Listener {
            override fun onPlaybackStateChanged(playbackState: Int) {
                mutableVideo.value = when (playbackState) {
                    Player.STATE_BUFFERING -> VideoState(VideoStatus.BUFFERING)
                    Player.STATE_READY, Player.STATE_ENDED -> VideoState(VideoStatus.PLAYING)
                    else -> mutableVideo.value
                }
            }
            override fun onPlayerError(error: PlaybackException) {
                mutableVideo.value = VideoState(VideoStatus.FAILED, unsupportedCodec = error.errorCode in setOf(
                    PlaybackException.ERROR_CODE_DECODER_INIT_FAILED, PlaybackException.ERROR_CODE_DECODING_FORMAT_UNSUPPORTED,
                    PlaybackException.ERROR_CODE_DECODING_FORMAT_EXCEEDS_CAPABILITIES))
            }
        })
        prepare()
    }
    init { scope.launch { while (isActive) {
        if (foreground) mutablePosition.value = ArchivePosition(player.currentPosition.coerceAtLeast(0), player.duration.coerceAtLeast(0), player.isPlaying)
        delay(250)
    } } }
    fun seek(milliseconds: Long) { player.seekTo(milliseconds.coerceIn(0, player.duration.coerceAtLeast(0))) }
    fun toggle() { intentToPlay = !player.playWhenReady; player.playWhenReady = intentToPlay }
    fun retry() { player.prepare(); player.playWhenReady = intentToPlay }
    fun foreground(active: Boolean) {
        if (foreground == active) return
        foreground = active
        if (!active) { intentToPlay = player.playWhenReady; player.pause(); player.stop() }
        else { player.prepare(); player.playWhenReady = intentToPlay }
    }
    fun close(): Long { val position = player.currentPosition; scope.cancel(); player.release(); return position }
}
