package com.souhail.vodcast.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

val VodRed = Color(0xFFE50914)
val VodAmber = Color(0xFFF5C518)
val VodBg = Color(0xFF08080B)
val VodSurface = Color(0xFF121218)
val VodSurface2 = Color(0xFF1C1C24)
val VodText = Color(0xFFF3F3F7)
val VodMuted = Color(0xFF9C9CA8)
val VodGreen = Color(0xFF4ADE80)

private val Colors = darkColorScheme(
    primary = VodRed,
    onPrimary = Color.White,
    secondary = VodAmber,
    onSecondary = Color.Black,
    tertiary = VodGreen,
    onTertiary = Color.Black,
    background = VodBg,
    onBackground = VodText,
    surface = VodSurface,
    onSurface = VodText,
    surfaceVariant = VodSurface2,
    onSurfaceVariant = VodMuted,
    outline = Color(0xFF2A2A34),
    error = Color(0xFFFF6B6B),
)

private val Type = Typography(
    displaySmall = TextStyle(
        fontFamily = FontFamily.Default,
        fontWeight = FontWeight.Black,
        fontSize = 32.sp,
        letterSpacing = (-0.6).sp,
        lineHeight = 36.sp,
    ),
    headlineMedium = TextStyle(fontWeight = FontWeight.Bold, fontSize = 24.sp, letterSpacing = (-0.3).sp),
    titleLarge = TextStyle(fontWeight = FontWeight.SemiBold, fontSize = 19.sp),
    titleMedium = TextStyle(fontWeight = FontWeight.SemiBold, fontSize = 15.sp),
    bodyMedium = TextStyle(fontSize = 14.sp, lineHeight = 20.sp),
    bodySmall = TextStyle(fontSize = 12.sp, lineHeight = 16.sp),
    labelLarge = TextStyle(fontWeight = FontWeight.SemiBold, fontSize = 13.sp),
    labelMedium = TextStyle(fontWeight = FontWeight.Medium, fontSize = 11.sp, letterSpacing = 0.5.sp),
)

@Composable
fun VodTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = Colors, typography = Type, content = content)
}
