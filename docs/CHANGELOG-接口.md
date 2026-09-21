# 接口变更记录（CHANGELOG-接口）

> 规则：**任何对 `rpi/health_monitor/hal/` 的修改**都要在这里记一笔，
> 并同步更新 [`02-接口规格说明书.md`](02-接口规格说明书.md)。
> 只记录**接口层面**的变化（数据模型、基类方法、异常、注册表结构），不记录内部实现细节。

## 变更类型标记

| 标记 | 含义 | 对驱动作者的影响 |
| --- | --- | --- |
| **BREAKING** | 破坏性变更 | 必须改代码 |
| **ADDED** | 新增（向后兼容） | 无需改动（新字段有默认值） |
| **CHANGED** | 语义变化 | 需要复核自己的实现是否仍符合语义 |
| **FIXED** | 修正契约中的错误 | 可能需要跟着修 |

---

## 1.0.1 —— 2026-09-21

初始契约冻结（本版本建立基线）。

### ADDED

- `hal/device.py`：`Device` 抽象基类（`open` / `read` / `close` + `describe` / `self_check` / `status`）；
  `OutputDevice`（额外要求 `send(command)`）。
- `hal/models.py`：`Sample` 及其子类 `VitalSignsSample` / `AmbientSample` / `PrecisionTempSample` /
  `RangeSample` / `MotionSample` / `ButtonEvent` / `DisplayStatus`；
  枚举 `DeviceKind` / `MotionState` / `ButtonAction` / `Severity` / `AlarmCode` / `CommandType`；
  `AlarmEvent` / `Command` 家族；`now_ts()`。
- `hal/exceptions.py`：`HealthMonitorError` 体系（含 `DeviceTimeout` 继承 `DeviceIOError` 的设计说明）。
- `hal/mock_bus.py`：`MockBus`（I2C/SPI 钩子、故障注入、操作流水）与 `RealBus`（薄封装）。
- `hal/registry.py`：`MANIFEST` / `DriverSpec` / `create_device()` / `snapshot()`。
- `hal/pins.py`：引脚映射单一来源（`BCM_TO_PHYSICAL` / `describe_pin()` / `find_conflicts()`）。

### ADDED（同版本内的增量，向后兼容）

- `net/webui.py` + `GET /`（同 `/status`、`/index.html`）：**状态网页**（HTML，只读，无外链依赖）。
  JSON 接口行为**完全未变**；手机 App 不需要它。带 `--token` 时该页返回 401 且不提供口令输入框。

- `core/rules.py` 的 `ReadingSnapshot` 增加 **`data_age_s`（`float | None`，默认 `None`）** 与
  **`data_stale_after_s`（`float`，默认 30.0）**，并新增只读属性 **`data_stale`**；
  `health_summary()` 的返回里相应多出 **`data_age_s`** 与 **`data_stale`** 两个字段。
  - **原因**：手机端此前只能看到一堆 `null`，无法区分"传感器没接"与"整个采集早就停了"。
    后者需要立刻提醒用户（服务活着、HTTP 通、但数据是旧的，最危险）。
  - **影响面**：`net/web.py` 的 `/api/v1/current` 与 `docs/05-安卓通信协议.md` 已同步；
    安卓端新增字段是**兼容**的（旧客户端忽略即可，协议 §6）。
    规则判定逻辑**未受影响**（陈旧样本依旧是 `None`，不会参与报警判定）。
  配套测试：`tests/core/test_rules.py::TestSnapshotSummary::test_新鲜度字段`、
  `tests/core/test_collector.py::test_新鲜度随采集更新` / `test_全部设备停摆后判为陈旧`。

### CHANGED（相对最初的接口草稿）

- `ButtonAction` 增加 `NONE`：**"本次没有事件"必须用 `NONE`，不许用 `RELEASE` 冒充**——
  业务层无法区分"真的抬起了"和"什么都没发生"。
- `AmbientSample` 增加 `is_cached: bool = False`：DHT11 因硬件限制（间隔 <2s）返回上次值时，
  必须标注为缓存值，**不允许把旧值当新数据上报**。采集器据此区分"刚测到"与"沿用旧值"。
- `MockBus.hook_spi` 的钩子**必须返回与输入等长**的字节（SPI 是全双工）；
  长度不一致时 `MockBus` 直接报错（钩子与驱动对不上要立刻暴露，而非静默截断）。

### 设计约定（不是代码，但属于契约）

1. **未测出 → `None`，绝不是 0**（0 bpm 会被判成"心率过低"，属误报）。
2. **缓存值要标注**（见上面的 `is_cached`）。
3. **`mock=True` 时绝不碰真实硬件、绝不执行外部命令**。
4. **失败必须留痕**（`_note_fault()`），不许静默失败。
5. **输出器件不认识的指令抛 `UnsupportedError`**，不许静默忽略。
6. `core/` 与 `net/` 中**不得出现具体驱动类名**（只认驱动名 + 数据模型）。

---

## 模板（新增条目时照抄）

```markdown
## <版本> —— <YYYY-MM-DD>

### BREAKING
- `hal/models.py`：`XxxSample.foo` 由 `int` 改为 `float`。原因：……。
  驱动作者需要：……

### ADDED
- `hal/models.py`：`XxxSample.bar: float | None = None`（向后兼容）。

### CHANGED
- `Device.read()` 现在要求……。

### 影响面
- 受影响的驱动：`sensors/a.py`、`outputs/b.py`
- 已通知：<姓名>
```
