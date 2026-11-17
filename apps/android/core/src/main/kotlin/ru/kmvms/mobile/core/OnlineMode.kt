package ru.kmvms.mobile.core

enum class OnlineMode(val apiKey: String) {
    FAST("fast"), STABLE("stable");
    companion object { fun from(value: String?) = if (value == "fast") FAST else STABLE }
}

/** NAS preference and PTZ intent are independent of a source/device incompatibility. */
fun onlineTransport(mode: OnlineMode, ptz: Boolean, incompatible: Boolean = false): LiveTransport =
    if (!incompatible && (mode == OnlineMode.FAST || ptz)) LiveTransport.WEBSOCKET_FMP4 else LiveTransport.HLS
