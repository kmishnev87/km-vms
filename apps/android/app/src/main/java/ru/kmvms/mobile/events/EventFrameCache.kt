package ru.kmvms.mobile.events

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.AtomicFile
import android.util.LruCache
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import ru.kmvms.mobile.diagnostics.DiagnosticEntry
import java.io.File

/** Same private JPEG for a saved profile's notification, visible rows and detail sheet. */
internal object EventFrameCache {
    private const val LIMIT = 128 * 1024
    private const val MAX_FILES = 64
    private const val TTL = 12 * 60 * 60 * 1000L
    private val stripes = Array(16) { Mutex() }
    private val epochs = mutableMapOf<String, Long>()
    private class Frame(val bitmap: Bitmap, val expires: Long)
    private val memory = object : LruCache<String, Frame>(6 * 1024 * 1024) {
        override fun sizeOf(key: String, value: Frame) = value.bitmap.allocationByteCount
    }
    private fun name(profile: String, id: String) = "${DiagnosticEntry.profileKey(profile)}-${validEventId(id)}.jpg"
    private fun dir(context: Context) = File(context.cacheDir, "event-jpeg")
    private val cachedName = Regex("[a-f0-9]{16}-[a-f0-9-]{36}\\.jpg")

    fun memory(profile: String?, id: String): Bitmap? {
        val key = profile?.let { name(it, id) } ?: return null
        return memory.get(key)?.takeIf { it.expires > System.currentTimeMillis() }?.bitmap
    }

    suspend fun load(context: Context, profile: String?, id: String, fetch: suspend () -> ByteArray): Bitmap? =
        withContext(Dispatchers.IO) {
            val owner = profile ?: return@withContext decode(fetch())
            val key = name(owner, id)
            val stripe = stripes[Math.floorMod(key.hashCode(), stripes.size)]
            stripe.withLock {
                val ownEpoch = synchronized(this@EventFrameCache) { epochs[owner] ?: 0L }
                memory(owner, id)?.let { return@withLock it }
                val path = File(dir(context), key)
                val cached = runCatching {
                    val modified = path.lastModified()
                    if (modified > System.currentTimeMillis() - TTL && path.length() in 1L..LIMIT.toLong())
                        decode(path.readBytes())?.let { Frame(it, modified + TTL) } else null
                }.getOrNull()
                if (cached != null) {
                    synchronized(this@EventFrameCache) {
                        if ((epochs[owner] ?: 0L) == ownEpoch) memory.put(key, cached)
                    }
                    return@withLock cached.bitmap
                }
                val bytes = fetch()
                val bitmap = decode(bytes) ?: return@withLock null
                currentCoroutineContext().ensureActive()
                synchronized(this@EventFrameCache) {
                    if ((epochs[owner] ?: 0L) == ownEpoch) {
                        memory.put(key, Frame(bitmap, System.currentTimeMillis() + TTL))
                        runCatching { save(path, bytes) }
                    }
                }
                bitmap
            }
        }

    private fun decode(jpeg: ByteArray): Bitmap? {
        if (jpeg.isEmpty() || jpeg.size > LIMIT) return null
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size, bounds)
        if (bounds.outWidth !in 1..800 || bounds.outHeight !in 1..600) return null
        return BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size)
    }

    private fun save(path: File, jpeg: ByteArray) {
        val folder = requireNotNull(path.parentFile)
        folder.mkdirs()
        val atomic = AtomicFile(path)
        val out = atomic.startWrite()
        try { out.write(jpeg); atomic.finishWrite(out) }
        catch (error: Exception) { atomic.failWrite(out); throw error }
        val now = System.currentTimeMillis()
        folder.listFiles().orEmpty().filter { cachedName.matches(it.name) && it.isFile }
            .sortedByDescending { it.lastModified() }.forEachIndexed { index, file ->
                if (index >= MAX_FILES || now - file.lastModified() > TTL) file.delete()
            }
    }

    @Synchronized fun clear(context: Context, profile: String) {
        epochs[profile] = (epochs[profile] ?: 0L) + 1
        val prefix = DiagnosticEntry.profileKey(profile) + "-"
        memory.snapshot().keys.filter { it.startsWith(prefix) }.forEach { memory.remove(it) }
        dir(context).listFiles().orEmpty().filter { it.name.startsWith(prefix) && cachedName.matches(it.name) }
            .forEach { AtomicFile(it).delete() }
    }
}
