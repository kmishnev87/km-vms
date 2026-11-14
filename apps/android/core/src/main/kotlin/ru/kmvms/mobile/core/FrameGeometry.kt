package ru.kmvms.mobile.core

data class FrameRect(val left: Double, val top: Double, val width: Double, val height: Double) {
    val right get() = left + width
    val bottom get() = top + height
    fun point(x: Double, y: Double): Pair<Double, Double>? =
        if (width > 0 && height > 0 && x in left..right && y in top..bottom)
            ((x - left) / width) to ((y - top) / height) else null
    fun selection(x0: Double, y0: Double, x1: Double, y1: Double): FrameRect? {
        val start = point(x0, y0) ?: return null
        val end = point(x1.coerceIn(left, right), y1.coerceIn(top, bottom)) ?: return null
        return FrameRect(minOf(start.first, end.first), minOf(start.second, end.second),
            kotlin.math.abs(start.first-end.first), kotlin.math.abs(start.second-end.second))
    }
}

fun fittedFrame(width: Double, height: Double, aspect: Double, scale: Double = 1.0,
                panX: Double = 0.0, panY: Double = 0.0): FrameRect {
    require(width >= 0 && height >= 0 && aspect > 0 && aspect.isFinite() && scale >= 1 && scale.isFinite())
    val fittedWidth = minOf(width, height * aspect)
    val fittedHeight = fittedWidth / aspect
    return FrameRect((width - fittedWidth*scale)/2 + panX, (height - fittedHeight*scale)/2 + panY,
        fittedWidth*scale, fittedHeight*scale)
}
