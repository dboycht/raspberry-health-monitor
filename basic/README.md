# 基础版 · 温湿度测量 + 动态曲线（课程作业 H）

> **这是"只做大作业"的最小可用版**：一个文件夹、三条命令。
> 它**不 import 主项目**（`rpi/health_monitor/`），整个 `basic/` 目录拷走就能交。
>
> 完整监护系统（心率/血氧/体温/活动/报警/上云/安卓 App）见仓库根
> [`README.md`](../README.md)；两者的关系见 [与完整版的关系](#8-与完整版的关系)。

---

## 1. 它做了什么（对着题目原文）

题目原文（H：温湿度测量）：

> 使用树莓派 5 和课程中学习过的温湿度传感器（如 DHT11）**周期性读取温度和湿度**。
> 程序**保存连续采集的数据**，并用 **Matplotlib 绘制动态曲线**；每得到一组新数据，
> 图中的曲线随之刷新。图中应**明确标出温度、湿度以及采样顺序或时间**。

| 题目要求 | 基础版的实现 | 在哪 |
| --- | --- | --- |
| 周期读取温湿度 | 默认 **3 秒**一次（DHT11 硬件要求 ≥2 秒，程序会自动抬高） | `basic/plot.py` 的 `Runner` |
| 保存连续采集的数据 | 每次采样**立刻写 CSV**（`flush + fsync`，拔电源也不丢） | `basic/store.py` |
| Matplotlib 动态曲线 | 温度（红，左轴 ℃）+ 湿度（蓝，右轴 %），每个采样点刷新一次 | `basic/plot.py` 的 `CurveWindow` |
| 标出温度、湿度 | 左轴 `温度 (℃)`、右轴 `湿度 (%)`，图例齐全 | 同上 |
| 标出采样顺序或时间 | 横轴可切：`--xaxis time`（相对秒数）/ `--xaxis index`（采样序号） | `basic/series.py` |
| 示例输出"温度: 25.0 ℃，湿度: 58 %" | 窗口标题实时显示 `最新一次读数　温度: 25.0 ℃　湿度: 58 %` | `basic/plot.py` |

验收清单与逐条判据：**[`验收说明.md`](验收说明.md)**。

---

## 2. 三条命令（**树莓派上**）

```bash
cd ~/raspberry-health-monitor/basic

sudo apt install -y python3-matplotlib python3-tk python3-lgpio fonts-noto-cjk   # 只需一次
python3 tools/selfcheck.py        # ① 先自检（10 项，不接硬件也能跑）
python3 run.py                    # ② 真实读取 DHT11（GPIO4 = 物理脚 7）→ 动态曲线窗口
python3 run.py --no-plot          # ③ 只想采数据不弹窗：只采集 + 存 CSV
```

**没有树莓派 / 没接传感器**（电脑上也能跑，用合成数据）：

```bash
cd basic
python run.py --mock              # 合成温湿度 → 同样能看动态曲线
python run.py --replay            # 回放仓库里的演示数据（验证"读 → 存 → 画"链路）
python tools/selfcheck.py         # 10 项自检
```

> 在电脑上跑千万不要去掉 `--mock`：电脑没有树莓派的 GPIO，会明确报
> "没有可用的 DHT11 读取后端"并告诉你该怎么办（**不会**偷偷用假数据糊过去）。

---

## 3. 接线（DHT11 → 树莓派 5 的 40-pin）

| DHT11 模块引脚 | 接到树莓派 | 说明 |
| --- | --- | --- |
| VCC（+） | **3.3V**（物理脚 1 或 17） | **不要接 5V**：数据电平会被拉到 5V，伤 GPIO |
| DATA（out） | **GPIO4 = 物理脚 7** | 默认数据脚（可用 `--pin` 改） |
| GND（−） | 任意 GND（物理脚 6/9/14/20/25/30/34/39，**推荐脚 6**） | 必须共地 |

⚠️ **四针裸传感器**必须在 DATA 与 3.3V 之间接一个 **4.7kΩ~10kΩ 上拉电阻**，
否则读数一直是失败（三针模块通常已自带）。

引脚号只信一张表（`basic/pins.py`）：GPIO4 = 物理脚 7、GPIO27 = 物理脚 13。
**BCM 编号与物理脚号不是偏移关系**，所以代码里不许写 `pin + 1` 这种换算。

🧾 **接线就看这一份**：[`hardware/接线表.md`](hardware/接线表.md) / [`hardware/接线表.pdf`](hardware/接线表.pdf)
—— **只有两张表**：① 树莓派接线（哪几个物理脚）② 元件接线（每个针脚插到哪）。
（每个数字都由代码生成，`python3 tools/wire_docs.py --check` 机器校验。）

📚 更细的历史资料（40-pin 全表 / 接线图与万用表逐项验证 / 供电安全 / 线色自查卡 / 合集 PDF）
在 [`hardware/_旧文档存档/`](hardware/_旧文档存档/)，**需要时再看**，上机不用带。

接线出问题时的定位命令：

```bash
python3 tools/diag_dht_line.py            # 数据线三态电平 → 直接给出"是哪类故障"
python3 tools/diag_dht_line.py --read 5   # 顺便连读 5 次真实数据
```

---

## 4. 目录地图

```
basic/
├── run.py                 入口：python3 run.py（把参数交给 plot.py）
├── plot.py                动态曲线 + 采集主循环 + 命令行参数
├── dht11read.py           DHT11 读取：lgpio 边沿时间戳 / gpiozero / mock 三种后端
├── pins.py                引脚映射（只查表，不写偏移公式）
├── wire_spec.py           ★接线事实来源（电源脚/地脚/上拉/周期 + 自检）
├── series.py              曲线数据窗口（滚动窗口 / 横轴 / 统计量，纯逻辑）
├── store.py               CSV 存档与**只读**读回（load_rows）
├── model.py               一条采样记录（Reading）：缺失值是 None，不写 0
├── data/
│   ├── sample_demo.csv    演示数据（合成，入库；没硬件的同学靠它跑通画图）
│   └── dht11_*.csv        运行时产生的数据（**不入库**，见 data/.gitignore）
├── evidence/curve_demo.png 曲线样张（截图证据，入库）
├── hardware/              ★**接线表**（由代码生成，勿手改）
│   ├── 接线表.md/.pdf       ★**两张表**：树莓派接线 + 元件接线（上机只看这**一份**）
│   └── _旧文档存档/         历史细节（40-pin 全表 / 接线图 / 供电安全 / 线色卡 / 合集），需要时再看
├── tools/
│   ├── selfcheck.py       10 项自检（不依赖硬件）
│   ├── wire_docs.py       接线表**生成 + 校验**（--generate / --check）
│   └── diag_dht_line.py   数据线诊断（三态电平 → 结论）
└── tests/                 117 项单测（不需要硬件）
```

> 🤖 `hardware/接线表.md` 由 `python3 tools/wire_docs.py --generate` 生成。
> **要改内容请改 `basic/wire_spec.py` 或生成器里的文案，再重新生成** —— 直接手改会被下次生成覆盖，
> 而且 `--check` 会报"接线表与代码不一致"。**硬件目录里只允许这一份 md**（多了会被 `--check` 抓到）。

---

## 5. 数据文件长什么样

`data/dht11_YYYYmmdd_HHMMSS.csv`（**一次运行 = 一个文件**，采样序号从 1 开始）：

```csv
index,timestamp,temperature_c,humidity_percent,status,note
1,2026-09-24 20:15:03.123,25.0,58.0,ok,
2,2026-09-24 20:15:06.118,25.1,57.8,ok,
3,2026-09-24 20:15:09.115,,,fail,只捕获到 0 个边沿（完整一帧约 82 个）：传感器没有应答
```

**读失败时留空、不写 0**（这是刻意的：0 ℃ 会被读成"结冰"，0 % 会被读成"极干燥"，
都是凭空编出来的数据）。同一目录还会写一个 `*.summary.json` 运行小结（采样次数、
成功/失败数、温度湿度范围、数据源），报告里可直接引用。

---

## 6. 程序怎么算"读到"/"读不到"（答辩会被问）

1. **单总线时序**：主机拉低 20 ms → 释放 → 传感器应答 → 40 个 bit（高电平 26 µs = 0、70 µs = 1）
   → 校验和；
2. **用 lgpio 的边沿时间戳**解宽度，而不是在 Python 里紧循环读电平
   （Python 抖动常大于 26 µs，会大量误码）；
3. **应答脉冲按位置丢掉**，不能靠"宽度阈值"过滤 —— 应答约 80 µs，和 bit"1"约 70 µs 几乎同宽；
4. **读取间隔 ≥2 秒**：DHT11 读太快会返回**上一次的陈旧数据**。基础版宁可报
   "距上次读取不足 X 秒"也不拿旧值冒充新值；
5. **校验和 / 量程双重把关**：校验和不符、温度不在 0~50 ℃、湿度不在 20~90 %RH
   一律判为"本次无效"（`status=fail`）。

想验证第 1~3 条不需要真传感器：`basic/tests/test_basic_frame.py` 用**合成时序**
（自己按协议造边沿）喂进解码器，断言解出的温湿度与原文一致；README 第 8 节的自检也做同样的事。

---

## 7. 常见问题（**按现象查**）

| 现象 | 原因与处理 |
| --- | --- |
| `❌ 打不开 DHT11：没有可用的 DHT11 读取后端` | 在电脑上跑（没有 GPIO）→ 加 `--mock`；树莓派上装 `sudo apt install -y python3-lgpio` |
| 一开始就读不到（`只捕获到 0 个边沿`） | 先跑 `python3 tools/diag_dht_line.py`（它会给出"是哪类故障"：拉死到地 / 拉死到 3.3V / 没有器件驱动）；再按 [`hardware/接线表.md`](hardware/接线表.md) 核对脚位（**最易犯**：脚 7 的邻居脚 6 是 GND，插错一格就成"拉死到地"） |
| 偶尔一次 `校验和不符` | DHT11 正常现象（重试 3 次会自动兜住）；杜邦线过长/接触不良会变频繁 |
| 每行都是 `fail` 且连续 5 次后程序停下 | 这是刻意的"不刷屏"设计；按上一条排查接线，再重新运行 |
| 窗口中文变成方框 | 装中文字体：`sudo apt install -y fonts-noto-cjk` |
| `ModuleNotFoundError: matplotlib` | `sudo apt install -y python3-matplotlib python3-tk`；不装也能 `--no-plot` 只采数据 |
| 树莓派没有显示器（SSH 里跑） | 用无窗口模式：`python3 run.py --duration 60 --save curve.png`（跑 60 秒后导出图片） |
| 想让曲线横轴显示采样序号 | `python3 run.py --xaxis index` |

> 更完整的"现象 → 原因 → 下一步"（含接线类故障与万用表逐项验证）见
> [`hardware/_旧文档存档/02-接线图.md`](hardware/_旧文档存档/02-接线图.md) 第 6 节与
> [`hardware/_旧文档存档/03-供电与安全.md`](hardware/_旧文档存档/03-供电与安全.md)。

---

## 8. 与完整版的关系

| | 基础版 `basic/` | 完整版 `rpi/` |
| --- | --- | --- |
| 目的 | **交课程作业 H**（温湿度 + 动态曲线） | 整套"居家老人监护系统"（课程设计团队作品） |
| 依赖 | 标准库 + matplotlib（可选） | HAL 契约层 + 12 个驱动 + HTTP API + 上云 + 安卓端 |
| 数据 | CSV | SQLite + HTTP 接口 + OneNET 云 |
| 代码量 | 8 个模块、117 项单测 | 40+ 个文件、597 项单测 |
| 谁能用 | 任何同学，拷贝即用 | 团队分工协作 |

两者是**同一套工程纪律的两个尺度**：基础版同样坚持"读不到就是 `None`、不补 0"、
"引脚只信一张表"、"先合成时序测协议再上真机"——所以从基础版升级到完整版时，
这些判断不会推翻重来。

---

## 9. 检查与单测怎么跑

```bash
cd raspberry-health-monitor        # 仓库根

python basic/tools/selfcheck.py              # 10 项自检（含接线事实与接线表）
python basic/tools/wire_docs.py --check      # 接线表与代码是否一致（含检查器注入自测）
python basic/tools/wire_docs.py --generate   # 改了引脚/接线事实后重新生成接线表
python -m pytest basic/tests -q              # 117 项单测（开发机实测）
python -m unittest discover -s basic/tests   # 不装 pytest 也能跑
```

提交前建议连主项目一起验（会把基础版自检 + 单测一起跑）：

```bash
cd rpi && python scripts/validate.py         # 11 项（含"基础版"这一项）
```
