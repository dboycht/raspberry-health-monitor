package com.dboycht.healthmonitor.ui.alarms

import com.dboycht.healthmonitor.data.MonitorApiResult
import com.dboycht.healthmonitor.domain.AlarmFeed
import com.dboycht.healthmonitor.domain.MonitorSnapshot
import com.dboycht.healthmonitor.domain.Severity
import com.dboycht.healthmonitor.testing.FakeMonitorRepository
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/** 报警页 ViewModel：横幅依据、消音、以及"长按后才发生的"SOS。 */
@OptIn(ExperimentalCoroutinesApi::class)
class AlarmsViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() = Dispatchers.setMain(dispatcher)

    @After
    fun tearDown() = Dispatchers.resetMain()

    private fun feed(): AlarmFeed = AlarmFeed(
        live = emptyList(),
        history = listOf(
            FakeMonitorRepository.event(code = "hr_too_high", ts = 100.0, severity = 2),
            FakeMonitorRepository.event(code = "sos_pressed", ts = 200.0, severity = 3),
            FakeMonitorRepository.event(code = "ambient_temp_high", ts = 300.0, severity = 1),
        ),
    )

    @Test
    fun `刷新后事件按严重度降序排列_并带上当前报警`() = runTest(dispatcher) {
        val repository = FakeMonitorRepository(
            alarms = MonitorApiResult.Success(feed()),
            current = MonitorApiResult.Success(
                MonitorSnapshot(activeAlarms = mapOf("spo2_too_low" to 1.0)),
            ),
        )
        val vm = AlarmsViewModel(repository)
        vm.refreshNow()
        advanceUntilIdle()

        val state = vm.uiState.value
        assertEquals(
            listOf("sos_pressed", "hr_too_high", "ambient_temp_high"),
            state.sortedHistory.map { it.code },
        )
        assertTrue(state.hasActiveAlarms)
        assertEquals(Severity.CRITICAL, state.maxSeverity)
    }

    @Test
    fun `接口失败时保留上次事件_并标记连接失败`() = runTest(dispatcher) {
        val repository = FakeMonitorRepository(alarms = MonitorApiResult.Success(feed()))
        val vm = AlarmsViewModel(repository)
        vm.refreshNow()
        advanceUntilIdle()
        assertEquals(3, vm.uiState.value.feed.history.size)

        repository.alarms = FakeMonitorRepository.failure("找不到该地址：请检查树莓派 IP 与手机是否在同一 WiFi")
        repository.current = MonitorApiResult.Success(MonitorSnapshot())
        vm.refreshNow()
        advanceUntilIdle()

        val state = vm.uiState.value
        assertEquals(3, state.feed.history.size) // 没被清空
        assertNotNull(state.errorMessage)
        assertTrue(state.errorMessage!!.contains("找不到该地址"))
    }

    @Test
    fun `消音反馈明确说明报警未解除`() = runTest(dispatcher) {
        val repository = FakeMonitorRepository()
        repository.silenceResult = MonitorApiResult.Success(1790001900.0)
        val vm = AlarmsViewModel(repository)

        vm.silence()
        advanceUntilIdle()

        assertEquals(1, repository.silenceCalls)
        val feedback = vm.uiState.value.feedback!!
        assertTrue(feedback.contains("消音"))
        assertTrue("必须提醒报警状态仍在：$feedback", feedback.contains("仍然存在"))
        assertEquals(Severity.INFO, vm.uiState.value.feedbackSeverity)
    }

    @Test
    fun `SOS 成功后有明确反馈_并且会立刻刷新报警`() = runTest(dispatcher) {
        val repository = FakeMonitorRepository()
        repository.sosResult = MonitorApiResult.Success("已收到紧急求助，请立即查看")
        val vm = AlarmsViewModel(repository)

        vm.sendSos()
        advanceUntilIdle()

        assertEquals(1, repository.sosCalls)
        val state = vm.uiState.value
        assertFalse(state.sendingSos)
        assertTrue("求助成功要有明显反馈：${state.feedback}", state.feedback!!.contains("求助已发送"))
        assertEquals(Severity.CRITICAL, state.feedbackSeverity)
    }

    @Test
    fun `SOS 失败时给出失败提示_不崩`() = runTest(dispatcher) {
        val repository = FakeMonitorRepository()
        repository.sosResult = FakeMonitorRepository.failure("连接被拒绝：树莓派上的服务可能没在运行")
        val vm = AlarmsViewModel(repository)

        vm.sendSos()
        advanceUntilIdle()

        val feedback = vm.uiState.value.feedback!!
        assertTrue(feedback.startsWith("❌ 求助发送失败"))
        assertFalse(vm.uiState.value.sendingSos)
    }

    @Test
    fun `清除反馈后横幅状态不受影响`() = runTest(dispatcher) {
        val repository = FakeMonitorRepository(
            current = MonitorApiResult.Success(MonitorSnapshot(activeAlarms = mapOf("sos_pressed" to 1.0))),
        )
        val vm = AlarmsViewModel(repository)
        vm.refreshNow()
        advanceUntilIdle()
        vm.sendSos()
        advanceUntilIdle()

        vm.clearFeedback()
        assertEquals(null, vm.uiState.value.feedback)
        assertTrue(vm.uiState.value.hasActiveAlarms)
    }
}
