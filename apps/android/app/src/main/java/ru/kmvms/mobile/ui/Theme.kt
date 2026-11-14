package ru.kmvms.mobile.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

val PageBackground = Color(0xFFEEF3F8)
val Ink = Color(0xFF0F172A)
val Muted = Color(0xFF64748B)
val Accent = Color(0xFF2563EB)
val SoftAccent = Color(0xFFE8F1FF)
val Outline = Color(0xFFDBE3EF)

@Composable fun KMVMSTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = lightColorScheme(primary = Accent, onPrimary = Color.White,
        primaryContainer = SoftAccent, onPrimaryContainer = Ink, background = PageBackground,
        secondary = Muted, onSecondary = Color.White, secondaryContainer = SoftAccent, onSecondaryContainer = Accent,
        tertiary = Accent, tertiaryContainer = SoftAccent, surfaceVariant = PageBackground,
        surface = Color.White, surfaceContainer = PageBackground, surfaceContainerHigh = PageBackground,
        surfaceContainerHighest = PageBackground, surfaceContainerLow = Color.White,
        surfaceContainerLowest = Color.White, surfaceTint = Accent, onSurface = Ink,
        onSurfaceVariant = Muted, outline = Outline), typography = Typography(), content = content)
}
