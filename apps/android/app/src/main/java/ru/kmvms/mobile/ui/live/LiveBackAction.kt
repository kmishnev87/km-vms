package ru.kmvms.mobile.ui.live

import ru.kmvms.mobile.events.MobileVideoRequest

internal enum class LiveBackAction { EXIT_TO_ORIGIN, LEAVE_FULLSCREEN, SHOW_CAMERA_LIST }

/** Back from an event never opens the unrelated saved multi-camera selection. */
internal fun liveBackAction(request: MobileVideoRequest?, immersive: Boolean): LiveBackAction = when {
    request?.returnToEvents == true -> LiveBackAction.EXIT_TO_ORIGIN
    request?.singleCamera == true && immersive -> LiveBackAction.LEAVE_FULLSCREEN
    request?.singleCamera == true -> LiveBackAction.EXIT_TO_ORIGIN
    else -> LiveBackAction.SHOW_CAMERA_LIST
}
