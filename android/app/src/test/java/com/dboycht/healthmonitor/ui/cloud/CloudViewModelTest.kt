package com.dboycht.healthmonitor.ui.cloud

import com.dboycht.healthmonitor.data.CloudRepository
import com.dboycht.healthmonitor.data.MonitorApiResult
import com.dboycht.healthmonitor.domain.CloudReading
import com.dboycht.healthmonitor.domain.CloudSnapshot
import com.dboycht.healthmonitor.settings.AppSettings
import com.dboycht.healthmonitor.settings.InMemorySettingsRepository
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
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/** 云端页 ViewModel：配置判定 / 成功与失败 / 保留上次数据 / "全都很旧"的提示。 */
@OptIn(ExperimentalCoroutinesApi::class)
class CloudViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() = Dispatchers.setMain(dispatcher)

    @After
    fun tearDown() = Dispatchers.resetMain()

    /** 假云端仓库：记调用次数，可按需回成功/失败。 */
    private class FakeCloudRepository(
        var result: MonitorApiResult<CloudSnapshot> = MonitorApiResult.Success(CloudSnapshot()),
    ) : CloudRepository {
        var calls = 0
        var lastKey: String? = null
        override suspend fun loadLatest(
            productId: String,
            deviceName: String,
            accessKey: String,
        ): MonitorApiResult<CloudSnapshot> {
            calls++
            lastKey = accessKey
            return result
        }
    }

    private fun configuredSettings() = AppSettings(
        cloudProductId = "vmkgy5EP2t",
        cloudDeviceName = "t1",
        cloudAccessKey = "cmFzcGJlcnJ5LXRlc3Q=",
    )

    private fun viewModel(
        cloud: FakeCloudRepository,
        settings: AppSettings,
    ) = CloudViewModel(cloud, InMemorySettingsRepository(settings))

    private fun snapshot(ageSeconds: Long?) = CloudSnapshot(
        readings = listOf(
            CloudReading("heart_rate", "心率", "89 bpm", "2026-09-30 21:38:13", ageSeconds),
        ),
        fetchedAtMillis = 1_790_868_623_000L,
        streamCount = 18,
    )

    @Test
    fun `没配云端时不发请求_只显示引导`() = runTest(dispatcher) {
        val cloud = FakeCloudRepository()
        val vm = viewModel(cloud, AppSettings())     // 三项都空

        vm.refreshNow()
        advanceUntilIdle()

        assertFalse(vm.uiState.value.configured)
        assertTrue(vm.uiState.value.readings.isEmpty())
        assertEquals("没配置就不该去请求云端", 0, cloud.calls)
        assertNull(vm.uiState.value.errorMessage)
    }

    @Test
    fun `配好了就拉取并把密钥传给仓库`() = runTest(dispatcher) {
        val cloud = FakeCloudRepository(MonitorApiResult.Success(snapshot(120)))
        val vm = viewModel(cloud, configuredSettings())

        vm.refreshNow()
        advanceUntilIdle()

        val state = vm.uiState.value
        assertTrue(state.configured)
        assertEquals(1, cloud.calls)
        assertEquals("cmFzcGJlcnJ5LXRlc3Q=", cloud.lastKey)
        assertEquals(1, state.readings.size)
        assertEquals(18, state.streamCount)
        assertNull(state.errorMessage)
        assertFalse("两分钟前的数据不算全旧", state.allStale)
    }

    @Test
    fun `失败时保留上次数据并给出原因`() = runTest(dispatcher) {
        val cloud = FakeCloudRepository(MonitorApiResult.Success(snapshot(60)))
        val vm = viewModel(cloud, configuredSettings())
        vm.refreshNow()
        advanceUntilIdle()
        assertEquals(1, vm.uiState.value.readings.size)

        cloud.result = MonitorApiResult.Failure("连不上云端：模拟断网")
        vm.refreshNow()
        advanceUntilIdle()

        val state = vm.uiState.value
        assertEquals("失败也要保留上次拿到的数据", 1, state.readings.size)
        assertTrue(state.errorMessage!!.contains("模拟断网"))
        assertFalse(state.loading)
    }

    @Test
    fun `全部数据都很旧时要提示_避免拿旧数据当此刻状态`() = runTest(dispatcher) {
        val cloud = FakeCloudRepository(
            MonitorApiResult.Success(snapshot(CloudUiState.STALE_AFTER_SECONDS + 1)),
        )
        val vm = viewModel(cloud, configuredSettings())
        vm.refreshNow()
        advanceUntilIdle()

        assertTrue("超过阈值就该提示", vm.uiState.value.allStale)
    }

    @Test
    fun `阈值边界_刚好等于阈值不算旧`() = runTest(dispatcher) {
        val cloud = FakeCloudRepository(
            MonitorApiResult.Success(snapshot(CloudUiState.STALE_AFTER_SECONDS)),
        )
        val vm = viewModel(cloud, configuredSettings())
        vm.refreshNow()
        advanceUntilIdle()
        assertFalse(vm.uiState.value.allStale)
    }

    @Test
    fun `刷新周期是 30 秒_比板子直连慢`() {
        assertEquals(30_000L, CloudViewModel.POLL_INTERVAL_MILLIS)
    }
}
