package com.dboycht.healthmonitor.ui.dashboard

import com.dboycht.healthmonitor.domain.ConnectionStatus
import com.dboycht.healthmonitor.domain.MonitorSnapshot
import com.dboycht.healthmonitor.domain.Severity
import com.dboycht.healthmonitor.settings.AppSettings
import com.dboycht.healthmonitor.testing.FakeMonitorRepository
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
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
        assertFalse(state.slowRetry)
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
    fun `连续失败达到上限后降速重试_而不是停掉轮询`() = runTest(dispatcher) {
        val repository = FakeMonitorRepository(current = FakeMonitorRepository.failure())
        val vm = viewModel(repository)

        repeat(DashboardViewModel.MAX_CONSECUTIVE_FAILURES) {
            vm.refreshNow()
            advanceUntilIdle()
        }

        val slowed = vm.uiState.value
        assertTrue("连续失败后应进入降速重试", slowed.slowRetry)
        assertEquals(DashboardViewModel.MAX_CONSECUTIVE_FAILURES, slowed.consecutiveFailures)
        val callsWhenSlowed = repository.currentCalls

        // 手动刷新把它复位；这次成功。
        repository.current = com.dboycht.healthmonitor.data.MonitorApiResult.Success(MonitorSnapshot())
        vm.refreshNow()
        advanceUntilIdle()

        val recovered = vm.uiState.value
        assertFalse(recovered.slowRetry)
        assertEquals(0, recovered.consecutiveFailures)
        assertEquals(ConnectionStatus.CONNECTED, recovered.connection)
        assertTrue(repository.currentCalls > callsWhenSlowed)
    }

    // ------------------------------------------------------------------
    // 轮询**循环**本身（2026-10-02 真机验收补）
    //
    // 为什么单独一组：上面所有测试都只调 refreshNow()，**从来没有真正跑过循环** ——
    // 于是"连续失败 → while 退出 → refreshNow 拉不起来 → 界面显示已连接却永远不刷新"
    // 这个真机上被抓到的缺陷，在单测里**完全看不见**。这组测试驱动真实循环 [pollLoop]。
    //
    // ⚠️ 直接调 `pollLoop()` 而不是 `startPolling(lifecycle)`：后者要 `LifecycleRegistry`，
    //    在纯 JVM 单测里需要 `Looper`（会 NPE）。循环与生命周期接线分开，正好让"循环会不会停住"
    //    这件事**可以被测**。
    // ------------------------------------------------------------------

    /**
     * 推进虚拟时间并让调度器把这一瞬间的协程跑完。
     *
     * ⚠️ 循环是**故意无限**的，所以这里**不能用 `advanceUntilIdle()`**（那会一直往下推进，
     * 直到 `runTest` 超时）—— 必须"走一步、跑一跑"。
     */
    private fun kotlinx.coroutines.test.TestScope.tick(millis: Long) {
        advanceTimeBy(millis)
        runCurrent()
    }

    @Test
    fun `循环里连续失败后仍在重试_不会永久停住`() = runTest(dispatcher) {
        val repository = FakeMonitorRepository(current = FakeMonitorRepository.failure())
        val vm = viewModel(repository)

        val job = launch { vm.pollLoop() }
        try {
            // 跑够 3 次失败（4 秒一次）→ 进入降速。
            repeat(5) { tick(DashboardViewModel.POLL_INTERVAL_MILLIS) }
            assertTrue("应已进入降速重试", vm.uiState.value.slowRetry)
            val callsAtSlow = repository.currentCalls

            // ★ 关键：降速之后**还会**继续请求（旧实现到这里就永远不动了）。
            repeat(4) { tick(DashboardViewModel.RETRY_INTERVAL_MILLIS) }
            assertTrue(
                "降速重试期间必须仍在请求：calls $callsAtSlow -> ${repository.currentCalls}",
                repository.currentCalls > callsAtSlow,
            )
        } finally {
            job.cancelAndJoin()
        }
    }

    @Test
    fun `树莓派恢复后循环自动接上_回到原来节奏`() = runTest(dispatcher) {
        val repository = FakeMonitorRepository(current = FakeMonitorRepository.failure())
        val vm = viewModel(repository)

        val job = launch { vm.pollLoop() }
        try {
            repeat(5) { tick(DashboardViewModel.POLL_INTERVAL_MILLIS) }
            assertTrue(vm.uiState.value.slowRetry)

            // 树莓派服务恢复（真机场景：重启服务 / WiFi 回来）。
            repository.current = com.dboycht.healthmonitor.data.MonitorApiResult.Success(
                MonitorSnapshot(ambientTempC = 23.0),
            )
            // 走完降速周期：**不需要**任何人工干预，循环自己就会再试一次。
            repeat(3) { tick(DashboardViewModel.RETRY_INTERVAL_MILLIS) }

            val state = vm.uiState.value
            assertEquals("恢复后应显示已连接", ConnectionStatus.CONNECTED, state.connection)
            assertFalse("恢复后应退出降速", state.slowRetry)
            assertEquals(0, state.consecutiveFailures)
            assertTrue("恢复后应拿到数据", state.snapshot.ambientTempC != null)
        } finally {
            job.cancelAndJoin()
        }
    }

    @Test
    fun `降速期间点刷新会立刻请求_不必等满一轮`() = runTest(dispatcher) {
        val repository = FakeMonitorRepository(current = FakeMonitorRepository.failure())
        val vm = viewModel(repository)
        val job = launch { vm.pollLoop() }
        try {
            repeat(5) { tick(DashboardViewModel.POLL_INTERVAL_MILLIS) }
            assertTrue(vm.uiState.value.slowRetry)
            val before = repository.currentCalls

            vm.refreshNow()
            // ★ 只推进 1 毫秒：如果还要等满 20 秒的降速周期，下面的断言就不会成立。
            tick(1)

            assertTrue("点刷新应立刻发起请求", repository.currentCalls > before)
            // ⚠️ 这一次刷新**还是失败**（仓库仍是 failure）⇒ 计数继续累加、降速**照旧生效** ——
            //    这是对的（网络确实还没好），所以这里断言的是"降速未解除"，而不是"已解除"。
            assertTrue("仍然失败时应保持降速", vm.uiState.value.slowRetry)

            // 网络恢复后再点一次：这次应真的回到正常节奏。
            repository.current = com.dboycht.healthmonitor.data.MonitorApiResult.Success(
                MonitorSnapshot(ambientTempC = 23.0),
            )
            vm.refreshNow()
            tick(1)
            assertFalse("恢复后点刷新应解除降速", vm.uiState.value.slowRetry)
            assertEquals(ConnectionStatus.CONNECTED, vm.uiState.value.connection)
        } finally {
            job.cancelAndJoin()
        }
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
