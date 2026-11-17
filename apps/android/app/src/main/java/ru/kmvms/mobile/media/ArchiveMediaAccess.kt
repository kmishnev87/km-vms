package ru.kmvms.mobile.media

import kotlinx.coroutines.runBlocking
import java.io.IOException
import java.io.InterruptedIOException

/**
 * One source/action in one MediaSource factory, not an installation-wide token cache.
 * Shared range readers reuse its lease only in the same authenticated NAS session.
 * Every HTTP range still passes the server's current user/permission checks.
 */
internal class ArchiveMediaAccess(
    private val sessionScope: () -> Any?,
    private val authorize: suspend () -> MediaLease,
    private val clockNanos: () -> Long = System::nanoTime,
) {
    private class Entry(val scope: Any, val lease: MediaLease, val requestedAt: Long) {
        fun reusable(now: Long) = now - requestedAt in 0 until lease.renewAfterMillis * 1_000_000
    }
    private var cached: Entry? = null

    /** Single flight also covers concurrent DataSources for the same MediaSource. */
    @Synchronized fun lease(): MediaLease {
        repeat(2) {
            val own = sessionScope() ?: run {
                cached = null
                throw IOException("Archive session is no longer active")
            }
            cached?.takeIf { it.scope === own && it.reusable(clockNanos()) }?.let { return it.lease }
            cached = null
            val requestedAt = clockNanos()
            val lease = try { runBlocking { authorize() } }
            catch (_: InterruptedException) {
                Thread.currentThread().interrupt()
                throw InterruptedIOException("Archive access request interrupted")
            } catch (_: Exception) { throw IOException("Archive access could not be confirmed") }
            // Authorization may have renewed the session. Never attach a late
            // credential to a signed-out or changed account; confirm it anew.
            if (sessionScope() === own) {
                cached = Entry(own, lease, requestedAt)
                return lease
            }
        }
        throw IOException("Archive session changed during authorization")
    }

    @Synchronized fun invalidate(lease: MediaLease) {
        if (cached?.lease === lease) cached = null
    }
}
