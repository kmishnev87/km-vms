package ru.kmvms.mobile.ui.camera

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.LruCache
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.PageBackground

internal class CameraCardPreviews(private val api: NasApi) {
    private val cache = object : LruCache<Long, Bitmap>(8 * 1024 * 1024) {
        override fun sizeOf(key: Long, value: Bitmap) = value.allocationByteCount
    }
    suspend fun get(id: Long): Bitmap = cache.get(id) ?: withContext(Dispatchers.IO) {
        val bytes = api.cameraPreview(id)
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        require(bounds.outWidth > 0 && bounds.outHeight > 0 && bounds.outWidth.toLong() * bounds.outHeight <= 30_000_000)
        val options = BitmapFactory.Options().apply {
            inSampleSize = 1
            while (maxOf(bounds.outWidth, bounds.outHeight) / inSampleSize > 640) inSampleSize *= 2
        }
        (BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options) ?: error("preview_unavailable")).also { cache.put(id, it) }
    }
    fun clear() { cache.evictAll() }
}

@Composable internal fun CameraCardPreview(previews: CameraCardPreviews, id: Long, modifier: Modifier) {
    var bitmap by remember(previews, id) { mutableStateOf<Bitmap?>(null) }
    var loading by remember(previews, id) { mutableStateOf(true) }
    LaunchedEffect(previews, id) {
        try { bitmap = previews.get(id) }
        catch (cancelled: CancellationException) { throw cancelled }
        catch (_: Exception) { bitmap = null }
        finally { loading = false }
    }
    Box(modifier.clip(RoundedCornerShape(9.dp)).background(PageBackground).testTag("camera-preview-$id"), contentAlignment = Alignment.Center) {
        bitmap?.let { Image(it.asImageBitmap(), stringResource(R.string.camera_preview), Modifier.fillMaxSize(), contentScale = ContentScale.Crop) }
            ?: if (loading) CircularProgressIndicator(strokeWidth = 2.dp) else Text(stringResource(R.string.camera_preview_missing), style = MaterialTheme.typography.labelSmall, color = Muted)
    }
}
