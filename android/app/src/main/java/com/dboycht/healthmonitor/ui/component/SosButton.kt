package com.dboycht.healthmonitor.ui.component

import android.content.Context
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.dboycht.healthmonitor.domain.Severity
import kotlinx.coroutines.delay

/** 需要按住多久才算触发（协议 §5.5 建议 1.5 秒）。 */
const val SOS_HOLD_MILLIS: Long = 1_500L

/** 文案里要显示"1.5"，但常量是整数毫秒，这里做个转换，避免写死两处。 */
private val SOS_HOLD_MILLIS_HINT: String = (SOS_HOLD_MILLIS / 1000.0).toString()

/**
 * 紧急求助按钮：**必须长按 1.5 秒**才触发（协议 §5.5，防误触）。
 *
 * 交互细节：
 *  - 按住时按钮从下往上"灌满"红色进度条，并每 0.5 秒轻震一下 —— 手指挡着屏幕
 *    也能感觉到倒计时；
 *  - 中途松手 = 取消，什么都不发；
 *  - 已触发后要重新长按，不会因为一直按着而重复发送。
 */
@Composable
fun SosButton(
    onTrigger: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    label: String = "紧急求助",
    hint: String = "长按 $SOS_HOLD_MILLIS_HINT 秒触发",
) {
    val context = LocalContext.current
    val currentOnTrigger by rememberUpdatedState(onTrigger)

    var progress by remember { mutableFloatStateOf(0f) }

    val urgent = Color(Severity.colorArgb(Severity.CRITICAL))
    val disabled = !enabled

    Box(
        modifier = modifier
            .fillMaxWidth()
            .height(64.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(if (disabled) Color(0xFFBDBDBD) else urgent.copy(alpha = 0.16f))
            .pointerInput(enabled) {
                if (!enabled) return@pointerInput
                detectTapGestures(
                    onPress = {
                        val started = System.currentTimeMillis()
                        var nextBuzz = 500L
                        // 按住期间累进度；手指抬起或手势结束时这段协程会被取消，
                        // 所以"中途松手"天然就等于取消，不会误发。
                        while (true) {
                            val elapsed = System.currentTimeMillis() - started
                            progress = (elapsed.toFloat() / SOS_HOLD_MILLIS).coerceIn(0f, 1f)
                            if (elapsed >= nextBuzz) {
                                vibrate(context, 25)
                                nextBuzz += 500L
                            }
                            if (elapsed >= SOS_HOLD_MILLIS) {
                                // 只在"按满 1.5 秒"这一刻触发一次。
                                progress = 0f
                                vibrate(context, 220)
                                currentOnTrigger()
                                break
                            }
                            delay(40)
                        }
                        tryAwaitRelease()
                        progress = 0f
                    },
                )
            },
        contentAlignment = Alignment.Center,
    ) {
        // 进度填充（从下往上）
        if (progress > 0f) {
            Box(
                Modifier
                    .fillMaxWidth()
                    .height((64 * progress).dp)
                    .align(Alignment.BottomCenter)
                    .background(urgent.copy(alpha = 0.45f)),
            )
        }
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            Text(
                text = if (disabled) "$label（未连接）" else label,
                fontSize = 20.sp,
                fontWeight = FontWeight.Bold,
                color = if (disabled) Color.White else urgent,
                textAlign = TextAlign.Center,
            )
            Text(
                text = if (progress > 0f) "请继续按住… ${(progress * 100).toInt()}%" else hint,
                style = MaterialTheme.typography.bodySmall,
                color = if (disabled) Color.White else urgent.copy(alpha = 0.85f),
                textAlign = TextAlign.Center,
            )
        }
    }
}

/** 用系统 Vibrator；没有震动器（模拟器常见）时安静跳过，绝不抛异常。 */
private fun vibrate(context: Context, millis: Long) {
    runCatching {
        val vibrator: Vibrator? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            (context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager)?.defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
        }
        if (vibrator == null || !vibrator.hasVibrator()) return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            vibrator.vibrate(VibrationEffect.createOneShot(millis, VibrationEffect.DEFAULT_AMPLITUDE))
        } else {
            @Suppress("DEPRECATION")
            vibrator.vibrate(millis)
        }
    }
}

/** 占满剩余空间的提示块（比如 SOS 已发送后的确认文案）。 */
@Composable
fun FullWidthNotice(text: String, severity: Int, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            .fillMaxSize()
            .background(Color(Severity.containerColorArgb(severity))),
        contentAlignment = Alignment.Center,
    ) {
        Text(text = text, color = Color(Severity.colorArgb(severity)))
    }
}
