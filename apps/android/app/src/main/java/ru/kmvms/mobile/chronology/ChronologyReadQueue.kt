package ru.kmvms.mobile.chronology

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/**
 * Metadata only, never a limit on selected cameras or video readers.
 * Match the HTTP client's existing per-host budget instead of another three-wide
 * gate. Queued interactive reads precede speculation; only one prefetch is active.
 */
internal class ChronologyReadQueue(private val parallelism: Int) {
    init { require(parallelism > 0) }
    private class Request(val prefetch: Boolean) {
        val ready = CompletableDeferred<Unit>()
        var admitted = false
    }
    private val mutex = Mutex()
    private val foreground = ArrayDeque<Request>()
    private val background = ArrayDeque<Request>()
    private var active = 0
    private var prefetchActive = false

    suspend fun <T> foreground(block: suspend () -> T): T = read(false, block)
    suspend fun <T> prefetch(block: suspend () -> T): T = read(true, block)

    private suspend fun <T> read(prefetch: Boolean, block: suspend () -> T): T {
        val request = Request(prefetch)
        try {
            mutex.withLock {
                (if (prefetch) background else foreground).addLast(request)
                dispatch()
            }
            request.ready.await()
            return block()
        } finally {
            // A seek/foreground exit cancels pending and admitted requests alike.
            withContext(NonCancellable) {
                mutex.withLock {
                    if (request.admitted) {
                        active--
                        if (prefetch) prefetchActive = false
                    } else (if (prefetch) background else foreground).remove(request)
                    dispatch()
                }
            }
        }
    }

    /** Caller holds mutex; callbacks are not run under the lock. */
    private fun dispatch() {
        while (active < parallelism) {
            val next = (foreground.removeFirstOrNull()
                ?: if (!prefetchActive) background.removeFirstOrNull() else null) ?: return
            next.admitted = true
            active++
            if (next.prefetch) prefetchActive = true
            next.ready.complete(Unit)
        }
    }
}
