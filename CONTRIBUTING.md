# 贡献指南

> 详细版在 [`contrib/团队分工与提交规范.md`](contrib/团队分工与提交规范.md)；
> 写驱动的具体规范在 [`docs/04-驱动开发规范与贡献指南.md`](docs/04-驱动开发规范与贡献指南.md)。
> 这里只放"最少必要"的几条，给第一次提 PR/提交的同学看。

## 1. 先跑起来（不需要任何硬件）

```bash
cd rpi
python -m pytest -q                     # 全部单测（也可 python -m unittest discover -s tests -v）
python scripts/validate.py              # 9 项提交前检查（含引脚撞脚与文档一致性）
python -m health_monitor demo           # 11 幕全链路演示（应当退出码 0）
```

## 2. 提交前必须过的三条

| # | 判据 | 命令 |
| --- | --- | --- |
| 1 | 全部单测通过 | `cd rpi && python -m pytest -q` |
| 2 | 9 项检查全 PASS（含**引脚冲突**与**文档一致性**） | `python scripts/validate.py` |
| 3 | 没有把运行时数据/产物带进提交 | `git status --porcelain` |

**禁止入库**：`rpi/data/`、`*.db`、`*.log`、`__pycache__/`、`.venv/`、`android/build/`、
`android/.gradle/`、`android/local.properties`、`DEVELOPMENT.md`、`ERROR.md`。

## 3. 提交信息格式

`<范围>: <做了什么>`，范围建议：`sensors(<驱动名>)` / `outputs(<驱动名>)` / `core` / `net` /
`android` / `docs` / `hardware`。

```
sensors(dht11): 修复间隔不足时误报故障，改为返回缓存值并标 is_cached
core(rules): 心率报警加入迟滞，避免阈值附近反复触发
docs(接口): 补充 /api/v1/current 的 null 渲染规则
```

## 4. 三条硬规矩（改错代价最大）

1. **不要改 `rpi/health_monitor/hal/`**（契约层）。确需修改：先改
   `docs/02-接口规格说明书.md` + `docs/CHANGELOG-接口.md`，通知全组，再改代码。
2. **改 HTTP 接口必须同步** `docs/05-安卓通信协议.md`（只改一边算缺陷）。
3. **加/改报警码必须同时改三处**：`rpi/health_monitor/hal/models.py` 的 `AlarmCode`、
   `docs/08-报警规则表.md`、安卓端 `AlarmCatalog.kt`。
   （`rpi/scripts/check_docs.py` 会在 CI 里帮你核对这三处是否一致。）

## 5. 团队分工

一个器件 = 一个文件 = 一个人；业务层不认识任何具体器件类，所以**互不冲突**。
没做好的器件可以用 `rpi/health_monitor/playback.py` 的模拟实现顶上，其他人照样联调。
进度表在 [`docs/03-器件任务书.md`](docs/03-器件任务书.md) §7。

## 6. 真机验收（有硬件时）

```bash
cd rpi
sudo python3 scripts/hardware_test.py        # 逐项过关，失败时直接给出排查命令
```

它会检查平台 / 内核设备节点 / 依赖库 / I2C 扫描（0x57、0x27）/ 逐器件数值合理性 /
输出器件（LED、LCD、蜂鸣器、音箱、按键，需人工确认）。

> ⚠️ **"单测通过" ≠ "真机通过"**：单测证明代码逻辑对，真机才能证明接线/地址/时序对。
