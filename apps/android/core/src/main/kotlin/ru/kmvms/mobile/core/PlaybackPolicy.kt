package ru.kmvms.mobile.core

enum class Stream(val apiKey: String) { MAIN("main"), SUB("sub") }
enum class LiveTransport { HLS, WEBSOCKET_FMP4 }

data class CameraPlayback(
    val cameraId: Long,
    val stream: Stream = Stream.SUB,
    val sound: Boolean = false,
    val ptz: Boolean = false,
) {
    init { require(cameraId > 0) }
    val transport: LiveTransport get() = if (ptz) LiveTransport.WEBSOCKET_FMP4 else LiveTransport.HLS
    fun selectStream(selected: Stream) = copy(stream = selected)
    fun setPtz(enabled: Boolean) = copy(ptz = enabled)
}

data class VisibleTile(val cameraId: Long, val offset: Int, val height: Int)

fun visibleCameraIds(tiles: List<VisibleTile>, start: Int, end: Int, active: Boolean): Set<Long> {
    if (!active || end <= start) return emptySet()
    return tiles.asSequence().filter { it.height > 0 && it.offset < end && it.offset.toLong() + it.height > start }
        .map { it.cameraId }.toSet()
}
