package ru.kmvms.mobile.core

enum class Stream(val apiKey: String) { MAIN("main"), SUB("sub") }
enum class LiveTransport { HLS, WEBSOCKET_FMP4 }

data class CameraPlayback(
    val cameraId: Long,
    val stream: Stream = Stream.SUB,
    val sound: Boolean = false,
    val ptz: Boolean = false,
    val talk: Boolean = false,
    val talkDuck: Boolean = false,
    val soundBeforeCall: Boolean? = null,
) {
    init { require(cameraId > 0) }
    val lowLatency: Boolean get() = ptz || talk
    val transport: LiveTransport get() = if (lowLatency) LiveTransport.WEBSOCKET_FMP4 else LiveTransport.HLS
    val audible: Boolean get() = sound && !talkDuck
    fun selectStream(selected: Stream) = copy(stream = selected)
    fun setPtz(enabled: Boolean) = copy(ptz = enabled)
    fun setTalk(enabled: Boolean, duck: Boolean = false): CameraPlayback = when {
        enabled && !talk -> copy(talk = true, talkDuck = duck, sound = true, soundBeforeCall = sound)
        enabled -> copy(talkDuck = duck)
        talk -> copy(talk = false, talkDuck = false, sound = soundBeforeCall ?: sound, soundBeforeCall = null)
        else -> copy(talkDuck = false, soundBeforeCall = null)
    }
}

data class VisibleTile(val cameraId: Long, val offset: Int, val height: Int)

fun visibleCameraIds(tiles: List<VisibleTile>, start: Int, end: Int, active: Boolean): Set<Long> {
    if (!active || end <= start) return emptySet()
    return tiles.asSequence().filter { it.height > 0 && it.offset < end && it.offset.toLong() + it.height > start }
        .map { it.cameraId }.toSet()
}
