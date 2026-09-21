# raspberry-health-monitor · 树莓派居家老人健康与安全监护系统

> 《Python编程与物联网应用入门》2026 年课程设计 —— **健康医疗物联网**方向
>
> 一个"床头监护仪"：树莓派 5 采集老人的**心率血氧 / 体温 / 环境温湿度 / 活动状态**，
> 本地判定异常并**蜂鸣 + 语音播报 + 状态灯 + 液晶显示**，同时通过 HTTP 上报，
> 子女用**安卓 App** 远程查看、消音或发起求助。

```
┌──────────────────────── 树莓派 5（本仓库 rpi/） ────────────────────────┐
│  传感器层     MAX30102 心率血氧 │ DHT11 温湿度 │ TMP36+MCP3002 精密体温   │
│               HC-SR501 人体活动 │（拓展）HC-SR04 超声波测距              │
│      ↓ 统一契约（hal：Device 基类 / 数据模型 / Mock 总线 / 注册表）      │
│  业务层       采集调度 → 报警规则引擎 → 报警下发（语音/蜂鸣/灯/LCD）      │
│      ↓                                   ↓ SQLite 历史                  │
│  接口层       HTTP API（/api/v1/*）                       ▲              │
└───────────────────────────────────────────────────────────┼─────────────┘
                                                            │ 局域网 WiFi
                                       ┌────────────────────┴──────────────┐
                                       │  安卓 App（本仓库 android/）        │
                                       │  监护面板 / 报警 / 消音 / 求助       │
                                       └───────────────────────────────────┘
```

---

## 1. 30 秒上手（**不需要任何硬件**）

```powershell
cd D:\code\DeepSeekHarness\raspberry-health-monitor\rpi

# ① 全体驱动体检（模拟模式：检查代码链路是否完好）
python -m health_monitor selfcheck --mock

# ② 看一遍完整演示：正常 → 心率异常 → 血氧过低 → 久无活动 → 求救 → 恢复 → 传感器故障
python -m health_monitor demo

# ③ 起一个可以配合手机 App 的服务（模拟数据）
python -m health_monitor serve --mock
#    然后浏览器打开 http://127.0.0.1:8080/            ← 状态网页（答辩现场可视化）
#    或 http://127.0.0.1:8080/api/v1/current          ← JSON 接口
```

跑测试：

```powershell
cd D:\code\DeepSeekHarness\raspberry-health-monitor\rpi
python -m pytest -q          # 全部单元测试（不需要硬件、不需要 pytest 之外的依赖）
```

> 没有 pytest 也能跑：`python -m unittest discover -s tests -v`（测试全部用标准库 `unittest` 编写，
> 因为树莓派上不应该为跑测试而额外装东西）。

## 2. 树莓派上的部署（有硬件时）

```bash
# 1) 系统准备：开启 I2C 与 SPI
sudo raspi-config      # Interface Options → I2C → Enable；SPI → Enable
sudo apt update
sudo apt install -y python3-smbus i2c-tools python3-gpiozero python3-lgpio espeak-ng alsa-utils
pip3 install smbus2 spidev        # 或 sudo apt install -y python3-smbus python3-spidev

# 2) 接线自检（先确认器件真的被系统看到）
i2cdetect -y 1        # 应能看到 MAX30102 的 0x57 与 LCD 的 0x27（或 0x3f）
ls /dev/spidev0.*     # 存在说明 SPI 已开

# 3) 复制代码并体检（真实硬件模式）
cd ~/raspberry-health-monitor/rpi
python3 -m health_monitor selfcheck --real     # 逐个器件 open + 自检，失败会给出排查线索

# 4) 正式运行
python3 -m health_monitor serve --real --port 8080
#    手机 App 里填 http://<树莓派IP>:8080 即可
```

**接线与引脚**：见 [`hardware/01-引脚分配表.md`](hardware/01-引脚分配表.md) 与
[`hardware/02-接线图.md`](hardware/02-接线图.md)。

**接完线请跑硬件验收测试单**（逐项过关，失败时直接给出"下一步查什么"）：

```bash
cd ~/raspberry-health-monitor/rpi
sudo python3 scripts/hardware_test.py              # 全量（会响蜂鸣器/闪灯，需你目视确认）
python3 scripts/hardware_test.py --skip-output     # 跳过发声/闪灯的项目
python3 scripts/hardware_test.py --json report.json  # 同时导出机器可读报告
```

它会检查：平台 → 内核设备节点（`/dev/i2c-1`、`/dev/spidev0.0`、gpiochip）→ 依赖库 →
**I2C 扫描**（能否看到 0x57 与 0x27）→ 逐器件**数值合理性**（心率 40~180、体温 15~45 等）→
输出器件（LED/LCD/蜂鸣器/音箱/按键，需人工确认）。

## 3. 目录结构（谁该看哪个文件）

```
raspberry-health-monitor/
├── rpi/                                   ← 树莓派端（Python 3.11+，纯标准库 + 系统包）
│   ├── health_monitor/
│   │   ├── hal/            ★契约层：Device 基类、数据模型、Mock 总线、驱动注册表
│   │   ├── sensors/          输入驱动：每个文件一个器件，互不依赖（**每人认领一个**）
│   │   ├── outputs/          输出驱动：LCD / 音箱 / 蜂鸣器 / LED
│   │   ├── core/             业务层：配置、采集调度、报警规则、报警下发、SQLite 存储
│   │   ├── net/web.py        HTTP API（安卓端就调它）
│   │   ├── playback.py       模拟器件 + "驱动缺席时退回模拟"的工厂（开发/演示用）
│   │   ├── demo.py           一键演示剧本
│   │   ├── service.py        装配与主循环（唯一"知道所有零件"的地方）
│   │   └── main.py           命令行入口（selfcheck / serve / demo / status / drivers）
│   ├── config/devices.json   设备与阈值配置（换器件不改代码）
│   └── tests/                单元测试（unittest；无需硬件）
├── android/                              ← 安卓监护 App（Kotlin + Jetpack Compose）
├── hardware/                             ← 硬件文档：引脚分配、接线图、供电与安全
├── docs/                                 ← 接口规格、报警规则、开发规范、通信协议
└── contrib/                              ← 团队分工与提交规范
```

## 4. 团队分工怎么协作（本项目的核心设计）

每个器件一个文件、一个负责人，**互不冲突**：

| 角色 | 负责文件 | 必须实现 |
| --- | --- | --- |
| 传感器 A | `sensors/max30102.py`、`sensors/dht11.py` | 继承 `Device`，实现 `open/read/close` |
| 传感器 B | `sensors/tmp36.py`、`sensors/mcp3002.py`、`sensors/hc_sr501.py` | 同上（TMP36 经 ADC 读） |
| 交互 | `outputs/lcd1602.py`、`outputs/buzzer.py`、`outputs/led.py`、`outputs/bt_speaker.py` | 继承 `OutputDevice`，实现 `send(command)` |
| 安卓 | `android/` | 按 `docs/05-安卓通信协议.md` 实现 |
| 文档/答辩 | `hardware/`、`docs/`、`contrib/` | 引脚表、接线图、报告素材 |

**为什么不会互相打架**：所有器件都必须满足 `hal/` 里的同一套契约，
业务层只认"驱动名 + 数据模型"，**不认识任何具体器件类**。
所以某个器件没做好时，用 `playback.py` 的模拟实现顶上，其他人照样能联调。

- 开发规范与"新驱动怎么写"：**[`docs/04-驱动开发规范与贡献指南.md`](docs/04-驱动开发规范与贡献指南.md)**
- 接口契约（改之前必读）：**[`docs/02-接口规格说明书.md`](docs/02-接口规格说明书.md)**
- 器件任务书（认领与验收）：**[`docs/03-器件任务书.md`](docs/03-器件任务书.md)**
- 团队协作与提交：**[`contrib/团队分工与提交规范.md`](contrib/团队分工与提交规范.md)**
- 项目管理（分工/进度/交付/答辩）：**[`docs/01-项目管理.md`](docs/01-项目管理.md)**
- 安卓端怎么跑：**[`docs/06-安卓开发指南.md`](docs/06-安卓开发指南.md)**
- 想继续加分（上云/MQTT/更多功能）：**[`docs/07-拓展与上云.md`](docs/07-拓展与上云.md)**

## 5. 核心设计（答辩会被问到的点）

1. **端-边-云三层**
   - **端**：传感器与执行器（MAX30102、DHT11、TMP36、HC-SR501、LCD、蜂鸣器、音箱、LED）
   - **边**：树莓派本地完成采集、判定、报警——**断网也能报警**（不依赖云端）
   - **云/远程**：HTTP API 供安卓 App 远程查看与控制（可选再接 MQTT 上云，见 `docs/07`）

2. **契约驱动（HAL）的意义**
   业务层完全不认识具体器件：换显示屏型号、换心率模块，**业务代码一行不改**。
   这也是"团队并行开发"的前提——每人只碰自己的文件。

3. **"数据缺失"与"数据正常"严格区分**（本项目的安全红线）
   - 传感器读不到 → 上报 `sensor_fault` 报警，**绝不产出"正常"结论**；
   - 超过 `3 × 读取周期` 没更新 → 该字段置 `null` 并标注"数据陈旧"，**不允许用旧值冒充当前状态**；
   - 没检测到手指 → 心率/血氧为 `null`（**不是 0**），手机端显示"请将手指放好"；
   - PIR 读失败 → `motion_state = unknown`，**不推断成"无人"**。

4. **报警不刷屏**：冷却时间（同一报警 300 秒内只提醒一次，可配）+
   **迟滞**（心率 111 触发、回落 108 才解除，避免阈值附近反复横跳）+
   **恢复事件**（`all_clear`，否则手机端会永远停在"报警中"）。

5. **可测试性**：每个驱动都有对应的 `MockBus` 钩子与假时钟，
   所有单测**不接硬件、不 sleep、不偶发失败**（见 `tests/`）。

## 6. 报警规则速查

| 报警 | 默认阈值 | 严重度 | 本地动作 |
| --- | --- | --- | --- |
| 心率过高 / 过低 | >110 / <50 bpm | 警告 | 蜂鸣 3 次 + 语音 + 黄灯 + LCD |
| 血氧过低 | <93% | **紧急** | 蜂鸣 4 次 + 语音 + 红灯 |
| 体温偏高 / 偏低 | >37.5 / <35.5 °C | 警告 | 蜂鸣 2 次 + 语音 + 黄灯 |
| 室温 / 湿度异常 | >30 / <16 °C；>80% | 提示 | 黄灯 + LCD |
| 长时间无活动 | 1800 秒无人体活动 | **紧急** | 蜂鸣 + 语音 + 红灯（疑似跌倒） |
| 夜间频繁起夜 | 22:00~06:00 内 ≥5 次 | 警告 | 语音提示 |
| 传感器故障 | 连续 3 次读取失败 | 警告 | 黄灯 + LCD "SENSOR FAULT" |
| 紧急求助 | 按键 / 手机 App | **紧急** | 蜂鸣 5 次 + 语音 + 红灯 + LCD "SOS" |

阈值全部在 `rpi/config/devices.json` 的 `thresholds` 里，改完重启即可。
> ⚠️ 本系统是**看护提示**工具，**不是医疗诊断设备**；文档与界面都不得宣称医疗结论。

## 7. 常用命令

| 命令 | 作用 |
| --- | --- |
| `python -m health_monitor selfcheck --mock` | 不接硬件的代码链路体检 |
| `python -m health_monitor selfcheck --real` | 真实硬件体检（树莓派上跑） |
| `sudo python3 scripts/hardware_test.py` | **真机验收测试单**（逐项过关 + 失败时给排查命令） |
| `python scripts/validate.py` | 提交前 8 项检查（体检 / 引脚冲突 / 单测 / 演示） |
| `python -m health_monitor serve --mock` | 模拟模式起服务（PC 上联调安卓端） |
| `python -m health_monitor serve --real` | 正式运行 |
| `python -m health_monitor demo` | 一键演完整个报警链路 |
| `python -m health_monitor drivers` | 列出所有驱动与负责人（分工用） |
| `python -m health_monitor status --mock` | 打印设备与配置状态 |
| `python -m pytest -q` | 跑全部单元测试 |

## 8. 版本

- 版本单一来源：`rpi/health_monitor/__init__.py` 的 `__version__`（HTTP `/api/v1/health` 会返回它）
- 当前版本：**1.0.1**

## 9. 验证基线（2026-09-21 实测）

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 树莓派端全量检查 | `cd rpi; python scripts/validate.py` | **8 项全 PASS** |
| 树莓派端单元测试 | `cd rpi; python -m pytest -q` | **429 passed, 1 skipped, 170 subtests** |
| 端到端演示 | `cd rpi; python -m health_monitor demo` | 11 幕跑完，退出码 0 |
| 驱动注册表 | `python -m health_monitor drivers` | 11 个驱动全部可构造 |
| 引脚冲突 | `python scripts/validate.py` 第 5 项 | 7 个独占引脚无冲突 |
| 安卓端单元测试 | 见 `docs/06-安卓开发指南.md` 的构建命令 | **78 passed** |
| 安卓端打包 | 同上（`assembleDebug`） | `app-debug.apk`，11.86 MB |

> ⚠️ **以上全部是 PC 上的"模拟/无硬件"验证**。真实器件读数、真实 I2C/SPI 时序、
> 蓝牙音箱配对、手机与树莓派的真实局域网往返**都还没验过**——
> 这些必须由真人接硬件后跑 `python -m health_monitor selfcheck --real` 与真机联调。

## 10. 许可

MIT License，详见 [`LICENSE`](LICENSE)。
