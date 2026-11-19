package ru.kmvms.mobile.core

import kotlin.math.abs

enum class CameraStep { PREVIOUS, NEXT }

/** Navigation never changes the selection, its order, or the archive clock. */
fun adjacentCamera(ids: List<Long>, current: Long, step: CameraStep): Long? {
    val ordered = ids.filter { it > 0 }.distinct()
    val index = ordered.indexOf(current)
    if (ordered.size < 2 || index < 0) return null
    val delta = if (step == CameraStep.NEXT) 1 else -1
    return ordered[(index + delta + ordered.size) % ordered.size]
}

fun cameraSwipeStep(dx: Float, dy: Float, threshold: Float): CameraStep? = when {
    abs(dx) < threshold || abs(dx) <= abs(dy) * 1.5f -> null
    dx < 0 -> CameraStep.NEXT
    else -> CameraStep.PREVIOUS
}
