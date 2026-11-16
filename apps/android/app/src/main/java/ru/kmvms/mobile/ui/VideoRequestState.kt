package ru.kmvms.mobile.ui

import androidx.compose.runtime.saveable.listSaver
import ru.kmvms.mobile.events.MobileVideoRequest

/** Preserve the launch mode through Activity recreation, not through leaving Live. */
internal val videoRequestSaver = listSaver<MobileVideoRequest?, Any>(
    save = { request -> request?.let { listOf(it.cameraId, it.cameraName, it.archiveTime ?: -1L, it.key, it.singleCamera) } ?: emptyList() },
    restore = { values -> if (values.isEmpty()) null else MobileVideoRequest(
        values[0] as Long, values[1] as String, (values[2] as Long).takeIf { it >= 0 }, values[3] as String, values[4] as Boolean) }
)
