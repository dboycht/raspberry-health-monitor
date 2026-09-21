package com.dboycht.healthmonitor.ui.dashboard

import com.dboycht.healthmonitor.domain.ConnectionStatus
import com.dboycht.healthmonitor.domain.MonitorSnapshot
import com.dboycht.healthmonitor.domain.Severity
import com.dboycht.healthmonitor.settings.AppSettings
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
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * 首页 ViewModel 的行为测试（**不碰网络**：用 [FakeMonitorRepository]）。
 *
 * 覆盖协议 §5.2 的两条要求：
 *  - 请求失败不抛异常、不弹框，保留上次数据并标注"可能已过期"；
 *  - 连接状态可观察（"连接中… / 已连接 / 连接失败"）。
 */
@OptIn(ExperimentalCoroutinesApi::class)
class DashboardViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private fun viewModel(repository: FakeMonitorRepository): DashboardViewModel =
        DashboardViewModel(
            repository = repository,
            settingsProvider = { AppSettings(baseUrl = "192.168.1.20") },
        )

    @Test
    fun `成功刷新后状态为已连接且带上快照`() = runTest(dispatcher) {
        val repository = FakeMonitorRepository(
            current = com.dboycht.healthmonitor.data.MonitorApiResult.Success(
                MonitorSnapshot(heartRateBpm = 72.4, spo2Percent = 97.9, fingerDetected = true),
            ),
        )
        val vm = viewModel(repository)

        vm.refreshNow()
        advanceUntilIdle()

        val state = vm.uiState.value
        assertEquals(ConnectionStatus.CONNECTED, state.connection)
        assertEquals(72.4, state.snapshot.heartRateBpm!!, 0.001)
        assertNull(state.errorMessage)
        assertEquals(1, repository.currentCalls)
        assertFalse(state.pollingStopped)
    }

    @Test
    fun `失败时保留上次数据并给出中文提示`() = runTest(dispatcher) {
        val repository = FakeMonitorRepository(
            current = com.dboycht.healthmonitor.data.MonitorApiResult.Success(
                MonitorSnapshot(heartRateBpm = 80.0, fingerDetected = true),
            ),
        )
        val vm = viewModel(repository)
        vm.refreshNow()
        advanceUntilIdle()

        // 第二次请求失败（比如手机走出了 WiFi 范围）。
        repository.current = FakeMonitorRepository.failure("连接超时：手机和树莓派可能不在同一网络")
        vm.refreshNow()
        advanceUntilIdle()

        val state = vm.uiState.value
        assertEquals(ConnectionStatus.FAILED, state.connection)
        assertNotNull(state.errorMessage)
        assertTrue(state.errorMessage!!.contains("超时"))
        // 关键：上一次的数值还在（不能因为失败就清成 0 或崩掉）。
        assertEquals(80.0, state.snapshot.heartRateBpm!!, 0.001)
        assertEquals(1, state.consecutiveFailures)
    }

    @Test
    fun `连续失败达到上限后自动停止轮询_点刷新可复位`() = runTest(dispatcher) {
        val repository = FakeMonitorRepository(current = FakeMonitorRepository.failure())
        val vm = viewModel(repository)

        repeat(DashboardViewModel.MAX_CONSECUTIVE_FAILURES) {
            vm.refreshNow()
            advanceUntilIdle()
        }

        val stopped = vm.uiState.value
        assertTrue("连续失败后应停止轮询", stopped.pollingStopped)
        assertEquals(DashboardViewModel.MAX_CONSECUTIVE_FAILURES, stopped.consecutiveFailures)
        val callsWhenStopped = repository.currentCalls

        // 手动刷新把它复位；这次成功。
        repository.current = com.dboycht.healthmonitor.data.MonitorApiResult.Success(MonitorSnapshot())
        vm.refreshNow()
        advanceUntilIdle()

        val recovered = vm.uiState.value
        assertFalse(recovered.pollingStopped)
        assertEquals(0, recovered.consecutiveFailures)
        assertEquals(ConnectionStatus.CONNECTED, recovered.connection)
        assertTrue(repository.currentCalls > callsWhenStopped)
    }

    @Test
    fun `消音反馈说明报警未解除_失败也有提示`() = runTest(dispatcher) {
        val repository = FakeMonitorRepository()
        val vm = viewModel(repository)

        repository.silenceResult = com.dboycht.healthmonitor.data.MonitorApiResult.Success(1790001900.0)
        vm.silence()
        advanceUntilIdle()
        val okFeedback = vm.uiState.value.silenceFeedback!!
        assertEquals(1, repository.silenceCalls)
        // 协议 §4.6：消音 ≠ 解除报警，文案必须说清楚。
        assertTrue("反馈要说明报警未解除：$okFeedback", okFeedback.contains("不响铃") || okFeedback.contains("未解除"))

        repository.silenceResult = FakeMonitorRepository.failure("连接被拒绝：树莓派上的服务可能没在运行")
        vm.silence()
        advanceUntilIdle()
        assertTrue(vm.uiState.value.silenceFeedback!!.startsWith("消音失败"))

        vm.clearSilenceFeedback()
        assertNull(vm.uiState.value.silenceFeedback)
    }

    @Test
    fun `stopPolling 会把连接状态改成未连接`() = runTest(dispatcher) {
        val vm = viewModel(FakeMonitorRepository())
        vm.refreshNow()
        advanceUntilIdle()
        assertEquals(ConnectionStatus.CONNECTED, vm.uiState.value.connection)

        vm.stopPolling()
        assertEquals(ConnectionStatus.IDLE, vm.uiState.value.connection)
    }

    @Test
    fun `快照里的 sensor_failures 会原样进入卡片警告`() = runTest(dispatcher) {
        val snapshot = MonitorSnapshot(sensorFailures = mapOf("vitals" to 3))
        val repository = FakeMonitorRepository(
            current = com.dboycht.healthmonitor.data.MonitorApiResult.Success(snapshot),
        )
        val vm = viewModel(repository)
        vm.refreshNow()
        advanceUntilIdle()

        assertEquals(
            "vitals 传感器异常（连续 3 次）",
            com.dboycht.healthmonitor.ui.dashboard.DashboardCards
                .sensorWarningText(vm.uiState.value.snapshot),
        )
        assertEquals(Severity.WARNING, Severity.WARNING)
    }
}
