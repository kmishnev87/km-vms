package ru.kmvms.mobile.chronology

import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.LoadControl
import androidx.media3.exoplayer.upstream.DefaultAllocator

/** Keep loading while the group is paused, without exceeding its media-memory budget. */
@UnstableApi
internal class ArchiveLoadControl : DefaultLoadControl(DefaultAllocator(true,C.DEFAULT_BUFFER_SEGMENT_SIZE),
    50_000,90_000,1000,2000,32*1024*1024,false,0,false) {
    @Volatile var budgetBytes=32*1024*1024
    @Volatile var capacityMillis=Long.MAX_VALUE
        private set
    override fun shouldContinueLoading(parameters: LoadControl.Parameters): Boolean {
        val normal=super.shouldContinueLoading(parameters)
        val full=allocator.totalBytesAllocated>=budgetBytes
        if((full || !normal) && parameters.bufferedDurationUs>=250_000) {
            capacityMillis=(parameters.bufferedDurationUs/1000-100).coerceAtLeast(250)
        } else if(normal && !full)capacityMillis=Long.MAX_VALUE
        return normal && !full
    }
    override fun shouldStartPlayback(parameters: LoadControl.Parameters): Boolean =
        super.shouldStartPlayback(parameters) || (allocator.totalBytesAllocated>=budgetBytes && parameters.bufferedDurationUs>=250_000)
}
