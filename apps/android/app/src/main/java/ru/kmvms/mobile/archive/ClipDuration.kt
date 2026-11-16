package ru.kmvms.mobile.archive

import java.util.Locale
import kotlin.math.roundToLong

/** A language-independent media clock, including seconds for long clips. */
fun clipDuration(seconds: Double?): String {
    if (seconds == null || !seconds.isFinite() || seconds <= 0) return "—"
    val total = seconds.roundToLong().coerceAtLeast(1)
    return if (total >= 3600) String.format(Locale.ROOT, "%d:%02d:%02d", total / 3600, total / 60 % 60, total % 60)
        else String.format(Locale.ROOT, "%d:%02d", total / 60, total % 60)
}
