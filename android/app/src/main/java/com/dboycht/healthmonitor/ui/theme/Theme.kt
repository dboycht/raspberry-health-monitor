package com.dboycht.healthmonitor.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * 主题色。刻意**不用动态取色（Material You）**：监护界面的报警红/警告橙必须
 * 在任何手机上长得一样，否则"红色=紧急"这个约定会被系统主题改掉。
 */
private val Pine = Color(0xFF0B6E4F)
private val PineLight = Color(0xFF3E9C7A)
private val Sand = Color(0xFFF3F5F4)
private val Urgent = Color(0xFFC62828)
private val Warn = Color(0xFFEF6C00)

private val LightColors = lightColorScheme(
    primary = Pine,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFD3EDE2),
    onPrimaryContainer = Color(0xFF00302A),
    secondary = PineLight,
    onSecondary = Color.White,
    error = Urgent,
    onError = Color.White,
    errorContainer = Color(0xFFFFDAD6),
    onErrorContainer = Color(0xFF410002),
    background = Sand,
    onBackground = Color(0xFF1A1C1B),
    surface = Color.White,
    onSurface = Color(0xFF1A1C1B),
    surfaceVariant = Color(0xFFE7EDEA),
    onSurfaceVariant = Color(0xFF44474A),
    outline = Color(0xFF8C918E),
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFF7CE3B6),
    onPrimary = Color(0xFF00382A),
    primaryContainer = Color(0xFF00513E),
    onPrimaryContainer = Color(0xFF9BFFD6),
    secondary = PineLight,
    error = Color(0xFFFFB4AB),
    onError = Color(0xFF690005),
    errorContainer = Color(0xFF93000A),
    onErrorContainer = Color(0xFFFFDAD6),
    background = Color(0xFF111412),
    onBackground = Color(0xFFE1E3E1),
    surface = Color(0xFF191C1B),
    onSurface = Color(0xFFE1E3E1),
    surfaceVariant = Color(0xFF3F4945),
    onSurfaceVariant = Color(0xFFBEC9C4),
)

/** 警告橙在 Material 调色板里没有对应槽位，单独暴露一个语义色。 */
val WarningColor: Color = Warn

@Composable
fun HealthMonitorTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    MaterialTheme(
        colorScheme = if (darkTheme) DarkColors else LightColors,
        typography = MaterialTheme.typography,
        content = content,
    )
}
