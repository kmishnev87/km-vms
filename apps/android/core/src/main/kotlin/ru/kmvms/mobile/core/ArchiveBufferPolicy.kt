package ru.kmvms.mobile.core

/** Archive recovery hysteresis. Targets are playout milliseconds, not a sleep. */
class ArchiveBufferPolicy {
    private var level=0
    private var stableMillis=0L
    val reserveMillis get()=when(level){0->500L;1->8_000L;2->12_000L;else->18_000L}
    fun reset(){level=0;stableMillis=0}
    fun starved(){level=(level+1).coerceAtMost(3);stableMillis=0}
    fun progressed(elapsed:Long){
        stableMillis+=elapsed.coerceIn(0,500)
        if(stableMillis>=60_000){level=(level-1).coerceAtLeast(0);stableMillis=0}
    }
    fun required(speed:Float,remaining:Long,capacity:Long=Long.MAX_VALUE):Long =
        minOf((reserveMillis*speed).toLong().coerceAtLeast(350),
            (remaining-30).coerceAtLeast(0),capacity.coerceAtLeast(250))
}
