package ru.kmvms.mobile.core

/** Progressive WS has no adaptive-live clock: explicitly keep it near the newest samples. */
fun lowLatencyPlaybackRate(bufferAheadMillis: Long, queuedMillis: Long): Float {
    val ahead = bufferAheadMillis.coerceAtLeast(0) + queuedMillis.coerceAtLeast(0)
    return when {
        ahead > 1500 -> 1.25f
        ahead > 850 -> 1.12f
        ahead > 400 -> 1.05f
        else -> 1f
    }
}
