package ru.kmvms.mobile.core

import java.io.IOException
import java.io.InterruptedIOException
import java.util.ArrayDeque
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/** Never accumulate seconds of PTZ latency. Overflow requires a fresh MP4 init. */
class LiveBytePipe(private val capacity: Int = 16 * 1024 * 1024,
                   private val maximumAgeNanos: Long = TimeUnit.SECONDS.toNanos(2),
                   private val clock: () -> Long = System::nanoTime) {
    private data class Packet(val bytes: ByteArray, val arrived: Long, var offset: Int = 0)
    private val lock = ReentrantLock()
    private val available = lock.newCondition()
    private val packets = ArrayDeque<Packet>()
    private var buffered = 0
    private var closed = false
    private var failure: IOException? = null
    @Volatile var rejectionReason: String = "none"
        private set
    fun queuedAgeMillis(): Long = lock.withLock { packets.peekFirst()?.let { (clock()-it.arrived)/1_000_000 } ?: 0 }
    fun offer(bytes: ByteArray): Boolean = lock.withLock {
        if (closed || failure != null) return false
        if (bytes.isEmpty()) return true
        if (bytes.size.toLong() + buffered > capacity || packets.peekFirst()?.let { clock() - it.arrived > maximumAgeNanos } == true) {
            rejectionReason = if (bytes.size.toLong() + buffered > capacity) "capacity" else "age"
            failLocked(IOException("Live consumer fell behind")); return false
        }
        packets.addLast(Packet(bytes, clock())); buffered += bytes.size; available.signalAll(); true
    }
    fun read(destination: ByteArray, offset: Int, length: Int, timeoutSeconds: Long = 20): Int = lock.withLock {
        require(offset >= 0 && length >= 0 && offset.toLong() + length <= destination.size)
        if (length == 0) return 0
        var remaining = TimeUnit.SECONDS.toNanos(timeoutSeconds)
        while (packets.isEmpty() && !closed && failure == null) {
            if (remaining <= 0) throw IOException("Live source stopped delivering media")
            remaining = try { available.awaitNanos(remaining) } catch (_: InterruptedException) {
                Thread.currentThread().interrupt(); throw InterruptedIOException("Live read cancelled")
            }
        }
        failure?.let { throw it }
        val packet = packets.peekFirst() ?: return -1
        if (clock() - packet.arrived > maximumAgeNanos) {
            rejectionReason = "age"
            failLocked(IOException("Live consumer fell behind")); throw failure!!
        }
        val count = minOf(length, packet.bytes.size - packet.offset)
        packet.bytes.copyInto(destination, offset, packet.offset, packet.offset + count)
        packet.offset += count; buffered -= count
        if (packet.offset == packet.bytes.size) packets.removeFirst()
        count
    }
    fun fail(error: IOException) = lock.withLock { failLocked(error) }
    private fun failLocked(error: IOException) {
        // Cancelling an incompatible socket also raises a generic network error.
        // Preserve the first cause so the player can choose the correct fallback.
        if (failure == null) failure = error
        packets.clear(); buffered = 0; available.signalAll()
    }
    fun close() = lock.withLock { closed = true; packets.clear(); buffered = 0; available.signalAll() }
}
