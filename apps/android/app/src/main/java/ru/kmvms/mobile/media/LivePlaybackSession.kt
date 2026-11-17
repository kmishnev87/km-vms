package ru.kmvms.mobile.media

import android.content.Context
import android.net.Uri
import android.util.Log
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.C
import androidx.media3.common.Tracks
import androidx.media3.datasource.HttpDataSource
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.hls.HlsMediaSource
import androidx.media3.exoplayer.source.ProgressiveMediaSource
import androidx.media3.exoplayer.upstream.DefaultLoadErrorHandlingPolicy
import androidx.media3.extractor.ExtractorsFactory
import androidx.media3.extractor.mp4.FragmentedMp4Extractor
import androidx.media3.extractor.text.SubtitleParser
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import ru.kmvms.mobile.core.CameraPlayback
import ru.kmvms.mobile.BuildConfig
import ru.kmvms.mobile.core.LiveTransport
import ru.kmvms.mobile.core.OnlineMode
import ru.kmvms.mobile.core.Stream
import ru.kmvms.mobile.core.onlineTransport
import ru.kmvms.mobile.core.lowLatencyPlaybackRate
import ru.kmvms.mobile.data.ApiProblem
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.data.NasException
import ru.kmvms.mobile.diagnostics.*

enum class VideoStatus { CONNECTING, BUFFERING, PLAYING, RECONNECTING, FAILED }
data class VideoState(val status: VideoStatus = VideoStatus.CONNECTING, val audio: Boolean? = null, val accessDenied: Boolean = false,
                      val unsupportedCodec: Boolean = false, val hlsFallback: Boolean = false)

/** One owner, even while moving between feed, portrait detail and fullscreen. */
@UnstableApi
class LivePlaybackSession(context: Context, private val api: NasApi, initial: CameraPlayback,
                          private val scope: CoroutineScope) {
    private val mutable = MutableStateFlow(VideoState())
    val state = mutable.asStateFlow()
    private val endpoint = LiveEndpoint(api)
    private val reconnect = Channel<Unit>(Channel.CONFLATED)
    private var selection = initial
    private var task: Job? = null
    private var released = false
    private var codecFailure = false
    private var usingSocket = false
    private var onlineMode = OnlineMode.STABLE
    private val incompatibleStreams = mutableSetOf<Stream>()
    val player: ExoPlayer = ExoPlayer.Builder(context.applicationContext)
        // A ceiling is not a target latency. A 500ms ceiling stalls progressive
        // MP4 extraction while the socket is still producing interdependent data.
        .setLoadControl(DefaultLoadControl.Builder().setBufferDurationsMs(100, 10_000, 60, 100)
            .setPrioritizeTimeOverSizeThresholds(true).setBackBuffer(0, false).build())
        .build().apply {
            playWhenReady = true
            volume = if ((selection.sound || selection.talk) && !selection.talkDuck) 1f else 0f
            addListener(object : Player.Listener {
                override fun onPlaybackStateChanged(playbackState: Int) {
                    if (playbackState == Player.STATE_BUFFERING) mutable.update { it.copy(status = VideoStatus.BUFFERING) }
                }
                override fun onRenderedFirstFrame() { mutable.update { it.copy(status = VideoStatus.PLAYING, accessDenied = false) } }
                override fun onTracksChanged(tracks: Tracks) {
                    mutable.update { it.copy(audio = tracks.groups.any { group -> group.type == C.TRACK_TYPE_AUDIO }) }
                }
                override fun onIsPlayingChanged(isPlaying: Boolean) {
                    if (isPlaying) mutable.update { it.copy(status = VideoStatus.PLAYING) }
                    else mutable.update { if (it.status == VideoStatus.PLAYING) it.copy(status = VideoStatus.BUFFERING) else it }
                }
                override fun onPlayerError(error: PlaybackException) {
                    AppDiagnostics.record(DiagnosticCode.LIVE_FAILED, DiagnosticArea.LIVE, camera = selection.cameraId, number = error.errorCode)
                    val http = generateSequence(error.cause) { it.cause }.filterIsInstance<HttpDataSource.InvalidResponseCodeException>().firstOrNull()?.responseCode
                    codecFailure = error.errorCode in setOf(PlaybackException.ERROR_CODE_DECODER_INIT_FAILED, PlaybackException.ERROR_CODE_DECODING_FORMAT_UNSUPPORTED,
                            PlaybackException.ERROR_CODE_DECODING_FORMAT_EXCEEDS_CAPABILITIES, PlaybackException.ERROR_CODE_PARSING_CONTAINER_UNSUPPORTED) ||
                        (usingSocket && error.errorCode == PlaybackException.ERROR_CODE_DECODING_FAILED) ||
                        generateSequence(error.cause) { it.cause }.any { it is LiveTransportIncompatibleException }
                    if (usingSocket && codecFailure && http !in setOf(401, 403, 404)) {
                        incompatibleStreams.add(selection.stream)
                        codecFailure = false
                        mutable.update { it.copy(status = VideoStatus.CONNECTING, hlsFallback = true, unsupportedCodec = false) }
                    }
                    if (codecFailure) mutable.update { it.copy(status = VideoStatus.FAILED, unsupportedCodec = true) }
                    debug("player=${error.errorCodeName} http=$http"); reconnect.trySend(Unit)
                }
            })
        }

    init { start() }
    fun configure(value: CameraPlayback) {
        if (released) return
        require(value.cameraId == selection.cameraId)
        val changed = value.stream != selection.stream ||
            onlineTransport(onlineMode, value.lowLatency, value.stream in incompatibleStreams) !=
            onlineTransport(onlineMode, selection.lowLatency, selection.stream in incompatibleStreams)
        selection = value; player.volume = if ((value.sound || value.talk) && !value.talkDuck) 1f else 0f
        if (changed) { player.stop(); start() }
        else mutable.update { it.copy(hlsFallback = (onlineMode == OnlineMode.FAST || value.lowLatency) && value.stream in incompatibleStreams) }
    }
    fun retry() { if (!released) start() }
    private fun start() {
        task?.cancel()
        codecFailure = false
        val own = selection
        task = scope.launch {
            var attempt = 0
            while (isActive) {
                var viewer: String? = null
                try {
                    while (reconnect.tryReceive().isSuccess) { }
                    mutable.value = VideoState(if (attempt == 0) VideoStatus.CONNECTING else VideoStatus.RECONNECTING,
                        hlsFallback = own.stream in incompatibleStreams && mutable.value.hlsFallback)
                    debug("authorizing")
                    val lease = endpoint.authorize(own.cameraId, own.stream)
                    onlineMode = lease.onlineMode
                    val lowLatency = onlineTransport(lease.onlineMode, own.lowLatency, own.stream in incompatibleStreams) == LiveTransport.WEBSOCKET_FMP4
                    usingSocket = lowLatency
                    mutable.update { it.copy(hlsFallback = !lowLatency && (lease.onlineMode == OnlineMode.FAST || own.lowLatency) && own.stream in incompatibleStreams) }
                    if (!lowLatency) {
                        debug("opening-viewer"); viewer = endpoint.open(own.cameraId, own.stream)
                        debug("awaiting-source"); endpoint.awaitReady(own.cameraId, own.stream, viewer)
                    }
                    ensureActive()
                    val path = "api/live/${own.cameraId}/${own.stream.apiKey}/" + if (lowLatency) "mse" else "index.m3u8"
                    val item = MediaItem.fromUri(Uri.parse(api.address.endpoint(path).toString()))
                    val socketFactory = if (lowLatency) WebSocketDataSource.Factory(api, own.cameraId, own.stream) { hasAudio ->
                        mutable.update { it.copy(audio = hasAudio) }
                    } else null
                    val source = if (socketFactory != null) ProgressiveMediaSource.Factory(socketFactory,
                        ExtractorsFactory { arrayOf(FragmentedMp4Extractor(SubtitleParser.Factory.UNSUPPORTED, FragmentedMp4Extractor.FLAG_EMIT_RAW_SUBTITLE_DATA)) })
                        .setContinueLoadingCheckIntervalBytes(32 * 1024)
                        .setLoadErrorHandlingPolicy(DefaultLoadErrorHandlingPolicy(0)).createMediaSource(item)
                    else HlsMediaSource.Factory(InstallationDataSource.Factory(api)).createMediaSource(item)
                    debug("preparing-${if (lowLatency) LiveTransport.WEBSOCKET_FMP4 else LiveTransport.HLS}")
                    player.setPlaybackSpeed(1f)
                    player.setMediaSource(source); player.prepare(); player.play()
                    coroutineScope {
                        if (socketFactory != null) launch {
                            var samples = 0
                            while (isActive) {
                                delay(200)
                                val buffered = (player.bufferedPosition-player.currentPosition).coerceAtLeast(0)
                                val queued = socketFactory.queuedAgeMillis()
                                val rate = lowLatencyPlaybackRate(buffered, queued)
                                if (player.playbackParameters.speed != rate) player.setPlaybackSpeed(rate)
                                if (++samples % 50 == 0) debug("timing-buffer=$buffered queue=$queued rate=$rate")
                            }
                        }
                        launch {
                            var current = lease
                            while (isActive) {
                                delay(current.renewAfterMillis)
                                // A healthy player is not reset when its five-minute authority is renewed.
                                val previousMode = current.onlineMode
                                current = renew(own, current)
                                debug("authority-renewed")
                                onlineMode = current.onlineMode
                                val nextTransport = onlineTransport(current.onlineMode, selection.lowLatency, own.stream in incompatibleStreams)
                                mutable.update { it.copy(hlsFallback = nextTransport == LiveTransport.HLS &&
                                    (current.onlineMode == OnlineMode.FAST || selection.lowLatency) && own.stream in incompatibleStreams) }
                                if ((nextTransport == LiveTransport.WEBSOCKET_FMP4) != lowLatency) reconnect.trySend(Unit)
                                if (previousMode != current.onlineMode) { reconnect.trySend(Unit); return@launch }
                                if (socketFactory != null && !socketFactory.refresh(current.token)) reconnect.trySend(Unit)
                            }
                        }
                        viewer?.let { id -> launch {
                            while (isActive) { delay(20_000); if (!endpoint.touch(id)) reconnect.trySend(Unit) }
                        } }
                        val ended = launch { reconnect.receive() }
                        ended.join()
                        coroutineContext.cancelChildren()
                    }
                    if (codecFailure) { mutable.value = VideoState(VideoStatus.FAILED, unsupportedCodec = true); break }
                } catch (cancelled: CancellationException) { throw cancelled }
                catch (error: Exception) {
                    debug("request-failed=${(error as? NasException)?.let { "${it.problem}:${it.httpStatus}" } ?: error.javaClass.simpleName}")
                    val denied = error is NasException && error.problem in setOf(ApiProblem.AUTHENTICATION, ApiProblem.FORBIDDEN)
                    mutable.value = VideoState(VideoStatus.FAILED, accessDenied = denied)
                    if (denied) break
                } finally {
                    if (isActive && !released) player.setPlaybackSpeed(1f)
                    viewer?.let { id -> withContext(NonCancellable) { withTimeoutOrNull(5000) { try { endpoint.close(id) } catch (_: Exception) { } } } }
                }
                attempt++
                delay(minOf(15_000L, 500L * (1L shl attempt.coerceAtMost(5))))
            }
        }
    }
    fun close() {
        if (released) return
        released = true; task?.cancel(); task = null; player.release()
    }
    private suspend fun renew(own: CameraPlayback, previous: MediaLease): MediaLease {
        while (currentCoroutineContext().isActive) {
            try { return endpoint.authorize(own.cameraId, own.stream) }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if (error is NasException && error.problem in setOf(ApiProblem.AUTHENTICATION, ApiProblem.FORBIDDEN) || !previous.canRetryRenewal()) throw error
                delay(5000)
            }
        }
        throw CancellationException("Live session closed")
    }
    private fun debug(message: String) { if (BuildConfig.DEBUG) Log.i("KMVMSVideo", "camera=${selection.cameraId} $message") }
}
