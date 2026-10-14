# 16 · 「GPIO 一律走 gpiozero」使用对照与自证

> **这一页解决什么问题**：老师要求用 `gpiozero`。这一页把"**哪里用了、写在哪一行、哪里没用、为什么**"
> 一次说清，并给出**可复制的命令**让人自己验 —— 而不是让你去翻源码或者听我口述。
>
> **最后更新**：2026-10-13　**口径来源**：课程要求（GPIO 访问统一用 `gpiozero`）

---

## 0. 一句话结论

- 本项目把 **`gpiozero` 作为 GPIO 访问层**：**按键、人体红外、蜂鸣器、三色 LED、TFT 的 DC/RST/背光**，
  以及 **DHT11**（在系统里的 gpiozero 还带 `DHT11` 类的平台上）**全部经 gpiozero**；
- 有 **3 处**不是 gpiozero，**都有平台/器件层面的硬原因**（不是偷懒，也不是漏改），逐条见 §2；
- 这句话**有机器守卫**，不是靠人记：`rpi/tests/scripts/test_gpiozero_policy.py`（§4）。

---

## 1. 逐器件对照（行号是 2026-10-13 的实读）

| 器件 | GPIO 访问用的库 | 位置（`import` 那一行） | 说明 |
| --- | --- | --- | --- |
| 按键（消音/求助、测血氧） | **`gpiozero.Button`** | `rpi/health_monitor/sensors/button.py:186` | 短按/长按都走 gpiozero 的事件回调 |
| HC-SR501 人体红外 | **`gpiozero.DigitalInputDevice`** | `rpi/health_monitor/sensors/hc_sr501.py:312` | 边沿计数 + 判"久无活动" |
| 三色 LED（红/黄/绿） | **`gpiozero.LED`** | `rpi/health_monitor/outputs/led.py:163` | 闪烁用 gpiozero 的**后台** `blink()`（不阻塞主循环） |
| 蜂鸣器 | **`gpiozero.DigitalOutputDevice`** | `rpi/health_monitor/outputs/buzzer.py:222` | 报警节奏由驱动折算成节拍表 |
| TFT 彩屏：**DC / RST / 背光**（+ 无 spidev 时的软件 SPI） | **`gpiozero.DigitalOutputDevice`** | `rpi/health_monitor/outputs/tft_spi.py:487` | ⭐ **2026-10-13 本轮从 lgpio 改过来的**（见 §3） |
| DHT11 温湿度（首选路径） | **`gpiozero.DHT11`** | `rpi/health_monitor/sensors/dht11.py:299` | 系统里没有这个类时自动回退（见 §2 第 1 条） |
| 基础版（作业 H）：DHT11 首选路径 | **`gpiozero.DHT11`** | `basic/dht11read.py:295`、`basic/hw/run.py:191` | 两份作业代码**都是 gpiozero 优先** |

> 📌 **`lgpio` 与 gpiozero 的关系（答辩常被追问）**：树莓派 5 上 `gpiozero` 的**底层引脚工厂就是
> `lgpio`**（`gpiozero` 2.x 在 Pi 5 上用 lgpio 后端）。所以系统里装着、用到 `lgpio` **不代表**
> "绕过了 gpiozero" —— 那是 gpiozero 的实现细节。本项目关心的是**代码里谁被调用**。

---

## 2. 三处"不是 gpiozero"，逐条给原因

| # | 位置 | 用的是什么 | **为什么不能用 gpiozero** |
| --- | --- | --- | --- |
| 1 | `rpi/health_monitor/sensors/dht11.py` 的**回退路径** | `lgpio`（内核级**边沿时间戳**） | **gpiozero 2.0.1 起删掉了 `DHT11`/`DHT22` 类**：真机（Debian 13 / trixie）上 `from gpiozero import DHT11` 直接 `ImportError`，包里连 `class DHT11` 都没有，`apt-cache search dht` 也没有任何相关包。⇒ 只能自实现单总线。**gpiozero 仍是首选**（该类存在时先用它）。完整排坑见开发副本 `ERROR.md` 的 **E27** |
| 2 | `rpi/health_monitor/outputs/tft_spi.py` 的 **SPI 数据线** | `spidev` | gpiozero 是"**接在 GPIO 上的简单器件**"抽象层，**没有** SPI 主控/显示屏驱动；ST7735/ST7789/ILI9341 这类要自己按数据手册发初始化序列与像素。⚠️ **控制脚（DC/RST/背光）已经全部走 gpiozero** —— 本轮改的就是这一半 |
| 3 | `rpi/health_monitor/sensors/max30102.py`、`rpi/health_monitor/outputs/lcd1602.py` | `smbus2`（经 HAL 的总线抽象） | 这两个是 **I2C 寄存器级器件**（写寄存器、读 FIFO）。**gpiozero 没有 I2C 寄存器 API**，它的 `gpiozero.…` 器件类不覆盖"按寄存器读写"这种用法 |

再说明两条**容易误判**的：

- **`RPi.GPIO` 一次都没用**（树莓派 5 上它不可用）—— 这条由守卫**明令禁止**（§4 第 1 条）；
- `basic/` 的两份作业代码里 `lgpio` 只作为**回退**存在（gpiozero 无 `DHT11` 类时才会用到）。

---

## 3. 本轮（2026-10-13）改了什么

| 文件 | 改动 | 判据 |
| --- | --- | --- |
| `rpi/health_monitor/outputs/tft_spi.py` | DC/RST/背光**以及**软件 SPI 的 SCLK/MOSI 全部改走 `gpiozero.DigitalOutputDevice`；文件**不再直接 `import lgpio`** | `rpi/tests/outputs/test_tft_spi.py::TestGpiozeroOnly`（7 项，含"有 spidev 时**绝不**把 SCLK/MOSI 抢成 GPIO"） |
| `basic/dht11read.py` | 后端顺序改为 **gpiozero 优先 → lgpio 回退**（原来是 lgpio 优先） | `basic/tests/test_basic_frame.py::TestBackendOrder`（3 项：两个都在选 gpiozero / 只有 lgpio 时回退 / 都没有时如实报错） |
| `basic/hw/run.py`（单文件作业版） | 新增 gpiozero 后端并置于首位；`read()` 按后端分派 | `basic/tests/test_basic_hw_run.py`（6 项） |
| 新增守卫 | `rpi/tests/scripts/test_gpiozero_policy.py` | 见 §4 |

> ⚠️ **顺序不等于技术排序**：单总线靠"高电平宽度"判 0/1，`lgpio` 能拿到**内核级边沿时间戳**，
> 时序上其实更稳。把 gpiozero 放第一位是**课程口径**，不是因为它在 DHT11 上更准 ——
> 这句话在两个基础版文件的注释里也写明了，免得下一个人误读。

---

## 4. 机器守卫：怎么自己验（一条命令）

```bash
cd rpi
python -m pytest tests/scripts/test_gpiozero_policy.py -q      # 期望：12 passed
```

它用 **AST 扫源码**（只看 `import`，不看注释/字符串 —— 本项目注释里到处都在解释为什么，
所以"提到 lgpio"不算违规），三条判据：

| # | 判据 | 反驳的正是 |
| --- | --- | --- |
| 1 | **不许 `import RPi` / `RPi.GPIO`** | "在 Pi 5 上抄一份老教程" |
| 2 | **直接接 GPIO 的驱动必须用到 `gpiozero`**（清单 `GPIO_DRIVERS`） | "新加了个器件，忘了走 gpiozero" |
| 3 | **直接 `import lgpio` 只允许白名单**（`LGPIO_WHITELIST`，当前只有 DHT11 一条，且**必须写明原因**） | "顺手用 lgpio 图快" |

另有三条**防腐烂**的检查：清单里的文件必须存在、白名单条目**必须仍然真的在 `import lgpio`**
（否则等于白名单挂着不动）、`basic/` 的作业版文件必须 `import gpiozero`。

守卫自己也有**注入自测**（`TestScannerItself`，6 项）：先证明"喂一份违规源码它真的抓得住"，
再看真仓库过不过 —— 因为**永远绿的守卫比没有守卫更糟**。

### 想手工再看一眼？（不依赖测试）

```bash
cd rpi
grep -rn "^ *from gpiozero import\|^ *import gpiozero" health_monitor    # gpiozero 用在哪几行
grep -rn "^ *import lgpio\|^ *from lgpio" health_monitor                 # lgpio 只剩 DHT11 一处
```

期望：第一条列出 §1 表里那 5 个驱动文件；第二条**只有** `sensors/dht11.py`。

---

## 5. ⚠️ 诚实边界：哪些还**没有真机验证**

改这一轮时**树莓派不在手边**（`192.168.50.3` 的 SSH 与 HTTP 都不通），所以：

| 项 | 状态 | 说明 |
| --- | --- | --- |
| TFT 控制脚改走 gpiozero 后的**真实出图** | ⬜ **未真机验证** | 代码路径有 mock 级单测（假 gpiozero 注入，含初始电平/按脚路由/释放）；**屏上出不出图必须上真机看** |
| **软件 SPI 回退**路径 | ⬜ **未真机验证**（且**历史上从未走到过**） | 板上一直有 `spidev`（`/dev/spidev0.*` 都在）⇒ 该回退从未被真实执行过。本轮只保证它走 gpiozero、位序与时钟有单测 |
| 基础版后端在真机上的**实际选择** | ⬜ 未真机验证 | 预期：Debian 13 的 gpiozero 无 `DHT11` 类 ⇒ 落到 `lgpio`，**行为与改动前一致**（顺序变了、结果不变） |
| 其余（按键/PIR/LED/蜂鸣器） | ✅ 原本就是 gpiozero | 本轮未改动它们的实现 |

**上电后的复验清单**（照抄即可）：

```bash
# ① 工程检查（期望 11/11）
python3 scripts/validate.py

# ② TFT 出图（人眼看三色条与文字方向；这一条替代不了真机）
python3 scripts/tft_check.py --controller auto

# ③ 逐器件体检（期望 8 个器件 OK）
python3 -m health_monitor selfcheck --real

# ④ 看 DHT11 到底走了哪个后端（日志里会出现一行「DHT11 使用 xxx 后端」）
python3 -m health_monitor serve --real
```

真机结果出来后，请把结论填进 [`13-真机接线现状与逐器件实测.md`](13-真机接线现状与逐器件实测.md)
与 [`07-测试验收记录.md`](07-测试验收记录.md) —— 本项目的规矩是**"单测通过" ≠ "真机通过"**。

---

## 6. 答辩可能被追问的三句

1. **"为什么不用 `RPi.GPIO`？"** —— 树莓派 5 换了 GPIO 架构，`RPi.GPIO` 在上面**不可用**；
   项目统一用 `gpiozero`（底层 `lgpio`）。守卫里明令禁止 `import RPi`。
2. **"gpiozero 有什么好处？"** —— ① 统一的 GPIO 访问层与器件语义（`Button`/`LED`/`Buzzer`…）；
   ② **引脚工厂可换**（lgpio / pigpio / mock），所以同一个驱动能在 PC 上用 mock 跑单测；
   ③ 内置后台闪烁、边沿回调等常用行为，少写易错的裸寄存器代码。
3. **"那 DHT11 为什么没有全用它？"** —— 不是没用，是**新版 gpiozero 把 `DHT11` 类删掉了**
   （真机实测：`ImportError`，包里连类都没有）。代码里它仍是**首选**，只是会**自动回退**到
   自实现的单总线（用 `lgpio` 的内核级边沿时间戳）。判据与排坑见 `ERROR.md` E27。
