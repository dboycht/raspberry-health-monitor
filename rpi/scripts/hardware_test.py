#!/usr/bin/env python3
"""真机硬件测试单：一条命令逐项验收，并给出"失败时下一步查什么"。

⚠️ **必须在树莓派上运行**（PC 上跑没有意义——PC 上没有 I2C/SPI/GPIO）。
在 PC 上请用 ``python -m health_monitor selfcheck --mock``。

它做什么（与 ``selfcheck --real`` 的区别）
------------------------------------------
``selfcheck`` 只回答"器件能不能打开、能不能读一次"；
本脚本进一步检查**物理层与协议层的前置条件**，并把每一项的排查动作写成
可直接复制粘贴的命令，减少"接上以后不知道从哪查"的时间：

1. 运行环境（是否树莓派 / Python 版本 / 是否 root 权限问题）
2. 内核接口是否就绪：`/dev/i2c-1`、`/dev/spidev0.*`、`/dev/gpiomem` 或 gpiochip
3. I2C 总线扫描（**关键**：能否看到 MAX30102 的 0x57 与 LCD 的 0x27/0x3F）
4. SPI 回环/读数（MCP3002 的原始值是否在合理范围，而不是恒 0/1023）
5. 逐器件读取：**数值是否物理合理**（心率 40~180、体温 30~42、体温与 ADC 原始值一致等）
6. 输出器件：LCD 显示、LED 亮灯、蜂鸣器响一声（**需要用户目视/耳听确认**）
7. 超声/拓展件（若启用）

用法::

    cd ~/raspberry-health-monitor/rpi
    sudo python3 scripts/hardware_test.py              # 全量
    python3 scripts/hardware_test.py --skip-output     # 不响蜂鸣器 / 不闪灯
    python3 scripts/hardware_test.py --interval 3      # 每项连续采样 3 次
    python3 scripts/hardware_test.py --json out.json   # 同时导出机器可读报告

    # 阶梯式验收（一次只加一个器件，见 docs/14-分步实施路线图.md）：
    python3 scripts/hardware_test.py --list            # 先看配置里有哪些设备名
    sudo python3 scripts/hardware_test.py --only ambient            # 只验 DHT11 这一级
    sudo python3 scripts/hardware_test.py --only ambient,display    # 也可以多个（逗号分隔）
    sudo python3 scripts/hardware_test.py --exclude speaker         # 排除某个器件

⚠️ `--only` 会**一并收窄前置检查**：只验 DHT11 时不再因为"没接 I2C 器件"而报 I2C 扫描失败
（否则阶梯式推进时满屏都是还没接的器件造成的噪音）。

退出码：0 = 全部通过；1 = 有失败项（报告里逐条给出排查动作）。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

# 让 `basic.console.safe_print` 可导入（打印 ✅/❌/⚠ 时在窄编码控制台上自动降级）
# ⚠️ 为什么（2026-09-25 真机实测，ERROR.md E32/E35）：中文 Windows / GBK 控制台上
#    `print` 直接打印这些符号时会抛 UnicodeEncodeError 把**整个脚本**崩掉；这些工具主要跑在
#    树莓派（UTF-8）上，导入失败就退回内置 print（行为与过去一致）。
try:
    from pathlib import Path  # noqa: E402
except ImportError:  # pragma: no cover - Path 是标准库，理论上不会失败
    Path = None
ROOT = Path(__file__).resolve().parents[2] if Path is not None else None
if ROOT is not None and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from basic.console import safe_print  # noqa: E402
except ImportError:  # pragma: no cover - 只在 basic 不可用时
    safe_print = print

RPI_DIR = Path(__file__).resolve().parents[1]
if str(RPI_DIR) not in sys.path:
    sys.path.insert(0, str(RPI_DIR))

# 结果分三档：PASS / WARN（能跑但可疑）/ FAIL（不可用）
PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


class Result:
    def __init__(self, name: str, status: str, detail: str, next_step: str = "") -> None:
        self.name = name
        self.status = status
        self.detail = detail
        self.next_step = next_step

    def as_dict(self) -> Dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "next_step": self.next_step}


class Reporter:
    def __init__(self, color: bool = True) -> None:
        self.results: List[Result] = []
        self.color = color and sys.stdout.isatty()

    def add(self, name: str, status: str, detail: str, next_step: str = "") -> Result:
        r = Result(name, status, detail, next_step)
        self.results.append(r)
        mark = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
        print(f"[{mark}] {name}")
        for line in detail.splitlines():
            print(f"       {line}")
        if next_step and status in (WARN, FAIL):
            print("       → 下一步：")
            for line in next_step.splitlines():
                print(f"         {line}")
        return r

    @property
    def failures(self) -> List[Result]:
        return [r for r in self.results if r.status == FAIL]

    @property
    def warnings(self) -> List[Result]:
        return [r for r in self.results if r.status == WARN]

    def summary(self) -> int:
        print("=" * 78)
        counts = {s: sum(1 for r in self.results if r.status == s) for s in (PASS, WARN, FAIL, SKIP)}
        print(f"结果：PASS {counts[PASS]} · WARN {counts[WARN]} · FAIL {counts[FAIL]} · SKIP {counts[SKIP]}")
        if self.warnings:
            safe_print("\n⚠️ 可疑项（能跑但读数不确定，建议人工复核）：")
            for r in self.warnings:
                print(f"   - {r.name}：{r.detail.splitlines()[0]}")
        if self.failures:
            safe_print("\n❌ 失败项与排查动作：")
            for r in self.failures:
                print(f"   - {r.name}：{r.detail.splitlines()[0]}")
                if r.next_step:
                    for line in r.next_step.splitlines():
                        print(f"       {line}")
            print("\n修完再跑一次本脚本；PC 端的 mock 测试与驱动代码无关，不受影响。")
        else:
            safe_print("\n✅ 树莓派端硬件验收全部通过。接下来：")
            print("   1) 启动完整服务：python3 -m health_monitor serve --real")
            print("   2) 手机 App 填 http://<树莓派IP>:8080 联调")
            print("   3) 把 docs/03-器件任务书.md §7 进度表里对应器件标为「真机通过」")
        print("=" * 78)
        return 1 if self.failures else 0


# --------------------------------------------------------------------------
# 1. 运行环境
# --------------------------------------------------------------------------


def detect_platform() -> Tuple[bool, str]:
    """判断是否在树莓派上（ARM + 树莓派型号文件）。"""
    machine = platform.machine().lower()
    arm = machine in ("aarch64", "armv7l", "armv6l", "arm64", "arm")
    model = ""
    for path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        try:
            model = Path(path).read_bytes().decode("utf-8", "replace").strip("\x00").strip()
            break
        except OSError:
            continue
    is_pi = arm and ("raspberry" in model.lower() or model == "")
    extra = f"机器架构 {platform.machine()}，型号 {model or '未知'}，Python {platform.python_version()}"
    return is_pi, extra


def check_devices() -> List[Tuple[str, bool, str]]:
    """检查 I2C / SPI / GPIO 设备节点是否存在。"""
    checks = [
        ("I2C 总线 /dev/i2c-1", "/dev/i2c-1"),
        ("SPI /dev/spidev0.0", "/dev/spidev0.0"),
    ]
    out: List[Tuple[str, bool, str]] = []
    for label, path in checks:
        exists = os.path.exists(path)
        out.append((label, exists, "存在" if exists else "不存在"))
    # GPIO：树莓派 5 用 gpiochip 字符设备；老型号是 /dev/gpiomem
    gpio_chars = sorted(Path("/dev").glob("gpiochip*"))
    has_gpiomem = os.path.exists("/dev/gpiomem")
    if gpio_chars or has_gpiomem:
        detail = f"gpiochip: {', '.join(p.name for p in gpio_chars) or '无'}" + \
                 ("；/dev/gpiomem 存在" if has_gpiomem else "")
        out.append(("GPIO 设备", True, detail))
    else:
        out.append(("GPIO 设备", False, "既没有 /dev/gpiochip* 也没有 /dev/gpiomem"))
    return out


def find_tool(name: str) -> str:
    """找可执行文件：先查 PATH，再查常见管理目录。

    ⚠️ 为什么不能只用 ``shutil.which``（2026-09-24 真机踩到）：
    ``i2cdetect`` 由 i2c-tools 装在 **/usr/sbin**，而普通用户（非 root）的 PATH
    在 Debian 上**不含 /usr/sbin**，于是 ``shutil.which("i2cdetect")`` 返回 None，
    体检就报"i2cdetect 不可用"——**明明装了却说没装**，很容易误导排查方向。
    """
    found = shutil.which(name)
    if found:
        return found
    for directory in ("/usr/sbin", "/sbin", "/usr/local/sbin", "/usr/bin", "/bin"):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


def check_tools() -> List[Tuple[str, bool, str, str]]:
    """检查外部工具/库是否可用（缺失时给出安装命令）。"""
    items: List[Tuple[str, bool, str, str]] = []
    for tool, install in (
        ("i2cdetect", "sudo apt install -y i2c-tools"),
        ("espeak-ng", "sudo apt install -y espeak-ng"),
        ("aplay", "sudo apt install -y alsa-utils"),
    ):
        found = find_tool(tool)
        items.append((tool, bool(found), found or "未找到", install))
    for module, install in (
        ("smbus2", "sudo apt install -y python3-smbus  或  pip3 install smbus2"),
        ("spidev", "sudo apt install -y python3-spidev 或  pip3 install spidev"),
        ("gpiozero", "sudo apt install -y python3-gpiozero python3-lgpio"),
    ):
        try:
            __import__(module)
            items.append((f"python 模块 {module}", True, "已安装", install))
        except ImportError:
            items.append((f"python 模块 {module}", False, "未安装", install))
    return items


def i2c_scan() -> Optional[Dict[int, str]]:
    """用 i2cdetect 扫描总线；返回 ``{地址: 原样文本}``。失败返回 None。"""
    tool = find_tool("i2cdetect")
    if not tool:
        return None
    try:
        proc = subprocess.run(
            [tool, "-y", "1"], capture_output=True, text=True, timeout=10, check=False
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    found: Dict[int, str] = {}
    for line in proc.stdout.splitlines():
        if ":" not in line:
            continue
        left, _, right = line.partition(":")
        try:
            base = int(left.strip(), 16)
        except ValueError:
            continue
        for idx, cell in enumerate(right.split()):
            cell = cell.strip()
            if cell and cell not in ("--",):
                try:
                    found[base + idx] = cell
                except Exception:  # noqa: BLE001
                    continue
    return found


# --------------------------------------------------------------------------
# 2. 逐器件数值合理性
# --------------------------------------------------------------------------


def sample_device(device: Any, times: int, gap_s: float = 1.2) -> List[Any]:
    """连续读 ``times`` 次，返回样本列表（失败的样本也返回，便于报告）。"""
    out = []
    for i in range(times):
        try:
            out.append(device.read())
        except Exception as exc:  # noqa: BLE001 - 单次失败也要记录
            out.append(exc)
        if i < times - 1:
            time.sleep(gap_s)
    return out


def evaluate_bounds(values: List[Optional[float]], low: float, high: float) -> Tuple[str, str]:
    """判定一组数值是否落在物理合理区间。返回 (状态, 说明)。"""
    valid = [v for v in values if v is not None]
    if not valid:
        return FAIL, "没有读到任何有效数值（全是 None 或读取失败）"
    bad = [v for v in valid if not (low <= v <= high)]
    if bad:
        return FAIL, f"有 {len(bad)}/{len(valid)} 个读数超出合理区间 [{low}, {high}]：{bad}"
    spread = max(valid) - min(valid)
    if spread > (high - low) * 0.5:
        return WARN, f"读数波动过大（{min(valid):.2f} ~ {max(valid):.2f}），建议再看几次"
    return PASS, f"读数 {min(valid):.2f} ~ {max(valid):.2f}（中位 {statistics.median(valid):.2f}）"


# --------------------------------------------------------------------------
# 检查范围（--only / --exclude）：阶梯式验收"只测我这一级新加的器件"
# --------------------------------------------------------------------------

#: 每个驱动需要哪些"总线 / 依赖 / 外部工具"检查。
#: 为什么要它（2026-09-26）：阶梯式推进时一次只加一个器件（见 `docs/14-分步实施路线图.md`），
#: 若仍按全量跑前置检查，**还没接的器件**会制造一堆失败/警告，把本级真正的信息埋掉。
#: ⚠️ 新增驱动时**必须**在这里登记；`tests/scripts/test_hardware_test_filters.py` 会检查
#: "注册表里的每个驱动都登记过"，漏登记就会红。
DRIVER_NEEDS: Dict[str, Set[str]] = {
    "max30102": {"i2c"},
    "lcd1602": {"i2c"},
    "tmp36": {"spi"},
    "mcp3002": {"spi"},
    "tft_spi": {"spi", "gpio"},
    "dht11": {"gpio"},
    "hc_sr501": {"gpio"},
    "hc_sr04": {"gpio"},
    "button": {"gpio"},
    "led": {"gpio"},
    "buzzer": {"gpio"},
    "bt_speaker": {"audio"},
}

#: 设备节点检查项（`check_devices()` 的标签）→ 需要哪一类
NODE_CHECK_NEEDS: Dict[str, str] = {
    "I2C": "i2c",
    "SPI": "spi",
    "GPIO": "gpio",
}

#: 工具 / Python 库检查项（`check_tools()` 的名字）→ 需要哪一类
TOOL_CHECK_NEEDS: Dict[str, str] = {
    "i2cdetect": "i2c",
    "smbus2": "i2c",
    "spidev": "spi",
    "gpiozero": "gpio",
    "espeak-ng": "audio",
    "aplay": "audio",
}

#: 本脚本**有为它写专门检查**的设备名（其余设备只做"能否打开"）。
#: 名字来自默认配置（`core/config.py::DEFAULT_CONFIG`）；改了设备名要同步这里，
#: 测试会断言"默认配置里的每个设备名要么在这里、要么在 COMPOSED_DEVICE_HINTS 里"。
CHECKED_DEVICE_NAMES: Set[str] = {
    "vitals", "body_temp", "ambient", "motion", "distance",
    "display", "status_led", "alarm_buzzer", "speaker", "sos_button",
}

#: 没有专门检查、但有**别的**验收办法的设备名 → 提示用哪个脚本/为什么
COMPOSED_DEVICE_HINTS: Dict[str, str] = {
    "tft": "TFT 的显示确认要用：python3 scripts/tft_check.py --steps（人眼确认颜色/方向）",
    "mcp3002": "MCP3002 由 tmp36 的检查覆盖；也可单独验通道：python3 scripts/diag_pin_levels.py --adc",
}


def parse_name_list(values: Optional[List[str]]) -> Set[str]:
    """把 ``--only a,b --only c`` 解析成 ``{"a","b","c"}``（去空白、去重）。"""
    out: Set[str] = set()
    for raw in values or []:
        for piece in str(raw).split(","):
            name = piece.strip()
            if name:
                out.add(name)
    return out


def needs_of(drivers: Iterable[str]) -> Set[str]:
    """这组驱动需要哪些检查类别（**未知驱动按 gpio 处理**：宁可多查，不可漏查）。"""
    needs: Set[str] = set()
    for driver in drivers:
        needs |= DRIVER_NEEDS.get(driver, {"gpio"})
    return needs


def select_devices(
    configured: List[Tuple[str, str]],
    enabled: Set[str],
    only: Set[str],
    exclude: Set[str],
) -> Tuple[List[str], List[str], List[str], List[str]]:
    """算出本次要检查哪些设备（纯函数，好测）。

    Args:
        configured: ``[(设备名, 驱动名), ...]``，**含** disabled 的。
        enabled: 配置里 ``enabled=true`` 的设备名集合。
        only: 只查这些（空集 = 不限制）。
        exclude: 排除这些。

    Returns:
        ``(selected, disabled_requested, unknown_only, unknown_exclude)``：
        - ``selected``：本次真正要检查的设备名（保持配置顺序）；
        - ``disabled_requested``：用户点名了、但在配置里是 disabled 的；
        - ``unknown_only`` / ``unknown_exclude``：配置里根本没有的名字（打错字要报错，
          否则会出现"什么都没查却报全绿"的假通过）。
    """
    names = [name for name, _ in configured]
    known = set(names)
    chosen = [
        name
        for name in names
        if (not only or name in only) and name not in exclude and name in enabled
    ]
    return (
        chosen,
        sorted((only & known) - enabled),
        sorted(only - known),
        sorted(exclude - known),
    )


def node_check_needed(label: str, needs: Set[str]) -> bool:
    """这条设备节点检查要不要跑（按标签里的 I2C/SPI/GPIO 关键字判定）。"""
    for keyword, need in NODE_CHECK_NEEDS.items():
        if keyword in label:
            return need in needs
    return True


def tool_check_needed(name: str, needs: Set[str]) -> bool:
    """这条工具/库检查要不要跑。"""
    for keyword, need in TOOL_CHECK_NEEDS.items():
        if keyword in name:
            return need in needs
    return True


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="树莓派端硬件验收测试单")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/devices.json）")
    parser.add_argument("--interval", type=int, default=3, help="每个器件连续采样次数（默认 3）")
    parser.add_argument("--skip-output", action="store_true", help="不做会发声/闪灯的检查")
    parser.add_argument("--json", default="", help="把报告写成 JSON 文件")
    parser.add_argument("--yes", action="store_true", help="跳过开头的确认提示（无人值守）")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="设备名",
        help="只检查这些设备（配置里的设备名，可逗号分隔、可重复）：--only ambient 或 --only ambient,display",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="设备名",
        help="排除这些设备（写法同 --only）",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="只列出配置里的设备与各自的检查类别，然后退出（用来查设备名）",
    )
    args = parser.parse_args()

    rep = Reporter()

    print("=" * 78)
    print("树莓派健康监护系统 · 真机硬件验收测试单")
    print("=" * 78)
    print("说明：本脚本会访问真实硬件；会发声/闪灯的检查可用 --skip-output 跳过。")
    print("每一项失败都会给出下一步的排查命令，照着做通常就能定位。\n")

    # ---------------- 0. 配置 + 本次检查范围 ----------------
    # ⚠️ 配置要在前置检查之前读：`--only` 需要先知道"这一步要验哪些器件"，
    #    才能把无关的总线/依赖检查一起收窄（否则阶梯式推进满屏噪音）。
    from health_monitor.core.config import load_config
    from health_monitor.hal import create_device

    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        rep.add("加载配置", FAIL, f"{type(exc).__name__}: {exc}", "检查 rpi/config/devices.json 是否存在且 JSON 合法")
        return rep.summary()

    configured = [(d.name, d.driver) for d in config.devices]
    enabled = {d.name for d in config.enabled_devices()}
    only, exclude = parse_name_list(args.only), parse_name_list(args.exclude)

    if args.list:
        print("配置里的设备（`*` = enabled=true）：\n")
        for name, driver in configured:
            mark = "*" if name in enabled else " "
            categories = ", ".join(sorted(needs_of([driver]))) or "-"
            print(f"  {mark} {name:<14s} {driver:<12s} 检查类别：{categories}")
        print("\n本次可用写法：")
        print("  sudo python3 scripts/hardware_test.py --only <设备名>[,<设备名>]")
        print("  sudo python3 scripts/hardware_test.py --exclude <设备名>")
        return 0

    selected, disabled_requested, unknown_only, unknown_exclude = select_devices(
        configured, enabled, only, exclude
    )
    if unknown_only or unknown_exclude:
        rep.add(
            "检查范围", FAIL,
            f"配置里没有这些设备名：--only {unknown_only} / --exclude {unknown_exclude}",
            "看一遍可用名字：python3 scripts/hardware_test.py --list\n"
            "（注意用的是**配置里的设备名**，例如 ambient / body_temp / vitals，不是驱动名 dht11）",
        )
        return rep.summary()
    if disabled_requested:
        rep.add(
            "检查范围", FAIL,
            f"这些设备在配置里是 enabled=false：{disabled_requested}",
            "先在 rpi/config/devices.json 里把它打开（enabled 改成 true）再跑；\n"
            "阶梯式推进时**一次只打开一个**，见 docs/14-分步实施路线图.md",
        )
        return rep.summary()
    if not selected:
        rep.add("检查范围", FAIL, "没有任何设备被选中（--only / --exclude 把范围清空了？）",
                "去掉限制跑全量，或用 --list 看设备名")
        return rep.summary()

    selected_drivers = [driver for name, driver in configured if name in set(selected)]
    needs = needs_of(selected_drivers)
    if only:
        scope = "--only " + ",".join(sorted(only))
    elif exclude:
        scope = "--exclude " + ",".join(sorted(exclude))
    else:
        scope = "全部已启用器件"
    print(f"本次范围：{scope}")
    print(f"  将检查 {len(selected)} 个设备：{', '.join(selected)}")
    print(f"  需要的检查类别：{', '.join(sorted(needs)) or '-'}")
    print()

    # ---------------- 1. 平台 ----------------
    is_pi, info = detect_platform()
    if is_pi:
        rep.add("运行平台", PASS, info)
    else:
        rep.add(
            "运行平台", FAIL,
            f"看起来不是树莓派：{info}",
            "本脚本必须在树莓派上运行。\n"
            "PC 上请用：python -m health_monitor selfcheck --mock",
        )

    # ---------------- 2. 设备节点（按本次范围收窄） ----------------
    for label, ok, detail in check_devices():
        if not node_check_needed(label, needs):
            continue
        if ok:
            rep.add(label, PASS, detail)
        else:
            next_step = (
                "开启 I2C：sudo raspi-config → Interface Options → I2C → Enable\n"
                "开启 SPI：sudo raspi-config → Interface Options → SPI → Enable\n"
                "改完需要重启：sudo reboot"
                if "SPI" in label or "I2C" in label
                else "树莓派 5 的 GPIO 走字符设备 /dev/gpiochip*；若都没有，检查系统是否被裁剪过"
            )
            rep.add(label, FAIL, detail, next_step)

    # ---------------- 3. 外部工具与库（按本次范围收窄） ----------------
    missing_tools = []
    missing_blockers: List[Tuple[str, str]] = []
    for name, ok, detail, install in check_tools():
        if not tool_check_needed(name, needs):
            continue
        if ok:
            rep.add(name, PASS, detail)
        else:
            missing_tools.append((name, install))
            blocker = name in ("python 模块 smbus2", "python 模块 spidev", "python 模块 gpiozero")
            if blocker:
                missing_blockers.append((name, install))
            rep.add(name, FAIL if blocker else WARN, detail, f"安装：{install}")

    # 前置门禁：缺关键库时不再逐器件报 9 遍同样的错（噪音会淹没真正的信息）
    if missing_blockers:
        rep.add(
            "依赖门禁", FAIL,
            f"缺少 {len(missing_blockers)} 个必需的 Python 库，后续器件检查无意义，已跳过：\n"
            + "\n".join(f"  - {n}：{i}" for n, i in missing_blockers),
            "一条命令装齐（推荐用系统包，省去编译）：\n"
            "  sudo apt update && sudo apt install -y python3-smbus python3-spidev python3-gpiozero python3-lgpio i2c-tools espeak-ng alsa-utils\n"
            "装完重跑本脚本：sudo python3 scripts/hardware_test.py",
        )
        code = rep.summary()
        if args.json:
            _write_json(args.json, rep)
        return code

    # ---------------- 4. I2C 扫描（只在本次范围包含 I2C 器件时做） ----------------
    if "i2c" not in needs:
        rep.add("I2C 总线扫描", SKIP, "本次范围不含 I2C 器件，跳过（--only/--exclude 收窄）")
    else:
        want_max = "max30102" in selected_drivers
        want_lcd = "lcd1602" in selected_drivers
        found = i2c_scan()
        if found is None:
            rep.add(
                "I2C 总线扫描", WARN, "i2cdetect 不可用或执行失败，跳过扫描",
                "安装 i2c-tools：sudo apt install -y i2c-tools\n然后手动跑：i2cdetect -y 1",
            )
        else:
            table = ", ".join(f"0x{addr:02X}" for addr in sorted(found)) or "（空）"
            critical = {0x57: "MAX30102 心率血氧"} if want_max else {}
            lcd_candidates = [0x27, 0x3F, 0x20, 0x38]
            missing_critical = [f"0x{a:02X}（{n}）" for a, n in critical.items() if a not in found]
            if missing_critical:
                rep.add(
                    "I2C 总线扫描", FAIL,
                    f"扫到的地址：{table}\n缺少：{', '.join(missing_critical)}",
                    "1) 检查 SDA=物理脚3、SCL=物理脚5、VCC/GND 是否接好（3.3V）\n"
                    "2) 单独只接这一个器件再扫（排除总线被拉死）\n"
                    "3) 换一根杜邦线；确认没有和 LCD 的地址冲突\n"
                    "4) 再扫一次：i2cdetect -y 1",
                )
            elif want_lcd and not any(a in found for a in lcd_candidates):
                rep.add(
                    "I2C 总线扫描", WARN,
                    f"扫到的地址：{table}\n未发现 LCD 转接板（候选 {[hex(a) for a in lcd_candidates]}）",
                    "检查 LCD 背面的 PCF8574 是否焊好、VCC 是 3.3V 还是 5V（两种都要试）\n"
                    "再看一次：i2cdetect -y 1",
                )
            else:
                rep.add("I2C 总线扫描", PASS, f"扫到的地址：{table}")

    # ---------------- 5. 装配真实设备（只装配本次选中的） ----------------
    devices: Dict[str, Any] = {}
    for name in selected:
        cfg = config.device(name)
        if cfg is None:  # pragma: no cover - select_devices 已保证存在
            continue
        try:
            dev = create_device(cfg.driver, params=cfg.params, mock=False, name=cfg.name)
            dev.open()
            devices[cfg.name] = dev
            rep.add(f"打开 {cfg.name}（{cfg.driver}）", PASS, dev.describe().get("bus", ""))
        except Exception as exc:  # noqa: BLE001
            hint = str(exc)
            rep.add(
                f"打开 {cfg.name}（{cfg.driver}）", FAIL, f"{type(exc).__name__}: {hint}",
                "错误信息里通常已经写了排查方向（本项目刻意把线索写进异常消息）\n"
                "通用三步：1) i2cdetect -y 1 看器件在不在；2) 复查供电与共地；3) 单独接该器件再试",
            )

    # ---------------- 6. 逐器件数值检查 ----------------
    def with_device(name: str) -> Optional[Any]:
        return devices.get(name)

    # 心率血氧
    vitals = with_device("vitals")
    if vitals is not None:
        samples = sample_device(vitals, args.interval, gap_s=2.0)
        hrs = [getattr(s, "heart_rate_bpm", None) for s in samples if not isinstance(s, Exception)]
        spo2s = [getattr(s, "spo2_percent", None) for s in samples if not isinstance(s, Exception)]
        fingers = [getattr(s, "finger_detected", None) for s in samples if not isinstance(s, Exception)]
        if any(fingers):
            status, detail = evaluate_bounds(hrs, 40.0, 180.0)
            rep.add(
                "心率数值（MAX30102）", status, detail,
                "手指要**完全覆盖**传感器窗口并保持不动 10 秒以上\n"
                "环境强光会干扰（可用手遮一下）；LED 电流可在配置里调 led_current\n"
                "读数恒为 None：先确认 I2C 0x57 能扫到，再看驱动日志",
            )
            status2, detail2 = evaluate_bounds(spo2s, 85.0, 100.0)
            rep.add("血氧数值（MAX30102）", status2, detail2, "同上；血氧经验式未做标准仪器标定，只需看量级")
        else:
            rep.add(
                "心率/血氧数值（MAX30102）", WARN,
                "没有检测到手指（finger_detected=False），本次跳过数值检查",
                "把手指完全覆盖传感器窗口，保持静止，然后重跑本脚本",
            )

    # 体温（TMP36 + MCP3002）
    body = with_device("body_temp")
    if body is not None:
        samples = sample_device(body, args.interval)
        temps = [getattr(s, "temperature_c", None) for s in samples if not isinstance(s, Exception)]
        raws = [getattr(s, "raw_adc", None) for s in samples if not isinstance(s, Exception)]
        status, detail = evaluate_bounds(temps, 15.0, 45.0)
        if status == PASS and raws:
            detail += f"；ADC 原始值 {min(r for r in raws if r is not None)}~{max(r for r in raws if r is not None)}"
        rep.add(
            "体温数值（TMP36+MCP3002）", status, detail,
            "⚠️ 裸 TMP36 是 SOT-23 表贴封装，必须焊在转接板上\n"
            "供电必须 3.3V（接 5V 会让输出超过 ADC 量程、读数饱和）\n"
            "读数恒 0 或 1023：先查 SPI 控制字与 CS（真机最常见的两个原因）\n"
            "换算公式：T = (raw/1023*3.3 - 0.75)*100 + 25；可用 calibration_offset_c 标定",
        )

    # 环境温湿度
    ambient = with_device("ambient")
    if ambient is not None:
        samples = sample_device(ambient, max(args.interval, 2), gap_s=2.5)
        temps = [getattr(s, "temperature_c", None) for s in samples if not isinstance(s, Exception)]
        hums = [getattr(s, "humidity_percent", None) for s in samples if not isinstance(s, Exception)]
        cached = [getattr(s, "is_cached", False) for s in samples if not isinstance(s, Exception)]
        status, detail = evaluate_bounds(temps, 0.0, 50.0)
        if all(cached):
            status, detail = WARN, "全部是缓存值（说明间隔不足 2 秒或器件一直读失败）"
        rep.add(
            "室温湿度（DHT11）", status, detail + f"；湿度 {[h for h in hums if h is not None]}",
            "DHT11 两次读取间隔必须 ≥2 秒（本脚本已按 2.5 秒间隔采）\n"
            "数据脚 GPIO4（物理脚 7），需 3.3V 供电与共地\n"
            "读不到先试 gpiozero 的 DHT11：python3 -c \"from gpiozero import DHT11; ...\"",
        )

    # 人体活动
    motion = with_device("motion")
    if motion is not None:
        samples = sample_device(motion, args.interval, gap_s=1.0)
        states = [getattr(s, "state", None) for s in samples if not isinstance(s, Exception)]
        detected = sum(1 for s in states if getattr(s, "value", "") == "detected")
        if detected:
            rep.add("人体活动（HC-SR501）", PASS, f"{detected}/{len(samples)} 次检测到人（有人在探测范围内即为正常）")
        else:
            rep.add(
                "人体活动（HC-SR501）", WARN,
                f"没有检测到人（{len(samples)} 次采样全是 idle/unknown）",
                "1) 上电后模块需要约 1 分钟稳定（本项可能只是还在热身）\n"
                "2) 走到传感器正前方 3 米内挥动手臂再试\n"
                "3) 确认 VCC 接的是 **5V**（物理脚 2），GND 共地，OUT 接 GPIO17（物理脚 11）\n"
                "4) 模块上两个电位器：调灵敏度与延时",
            )

    # 距离（拓展件）
    distance = with_device("distance")
    if distance is not None:
        samples = sample_device(distance, args.interval, gap_s=0.6)
        values = [getattr(s, "distance_cm", None) for s in samples if not isinstance(s, Exception)]
        status, detail = evaluate_bounds(values, 2.0, 400.0)
        rep.add(
            "超声波测距（HC-SR04）", status, detail,
            "⚠️ ECHO 输出 5V，必须经 TXS0102 或 1kΩ+2kΩ 分压后才能接 GPIO6（物理脚 31）\n"
            "VCC 必须 5V；TRIG=GPIO5（物理脚 29）\n"
            "读数恒 None：多为回波超时（目标太远/太软/角度太偏）",
        )

    # ---------------- 7. 输出器件 ----------------
    if args.skip_output:
        rep.add("输出器件检查", SKIP, "按 --skip-output 跳过（不会发声/闪灯）")
    else:
        led = devices.get("status_led")
        if led is not None:
            try:
                from health_monitor.hal.models import LightCommand

                print("\n>>> 观察 LED：现在依次点亮 绿 → 黄 → 红（各 1.5 秒）")
                for color in ("green", "yellow", "red"):
                    led.send(LightCommand(color=color))
                    time.sleep(1.5)
                led.send(LightCommand(color="off"))
                ok = input("    三种颜色都亮了吗？[y/N] ").strip().lower().startswith("y")
                rep.add(
                    "LED 三色指示", PASS if ok else FAIL,
                    "用户确认三种颜色都亮" if ok else "用户报告没有全亮",
                    "每路 LED 都要串 220Ω~1kΩ 限流电阻，负极接 GND\n"
                    "共阳 LED 需要在配置里设 active_low=true\n"
                    "引脚：绿=GPIO22（脚15）、黄=GPIO23（脚16）、红=GPIO24（脚18）",
                )
            except Exception as exc:  # noqa: BLE001
                rep.add("LED 三色指示", FAIL, f"{type(exc).__name__}: {exc}", "检查 GPIO 权限与引脚配置")

        lcd = devices.get("display")
        if lcd is not None:
            try:
                from health_monitor.hal.models import DisplayCommand

                print("\n>>> 观察 LCD：应显示两行测试文本")
                lcd.send(DisplayCommand(lines=("RPI HEALTH TEST", "LCD OK 1.0.1")))
                ok = input("    LCD 上显示的是这两行英文吗？[y/N] ").strip().lower().startswith("y")
                rep.add(
                    "LCD1602 显示", PASS if ok else FAIL,
                    "用户确认显示正确" if ok else "用户报告显示不对（可能全黑/花屏/乱码）",
                    "全黑：调背面的对比度电位器，或确认 VCC 电压（3.3V/5V 都试）\n"
                    "花屏：初始化序列或地址不对；驱动会自动探测 0x27/0x3F/0x20/0x38\n"
                    "显示乱码：**中文显示不了**（LCD1602 只有 ASCII 字库），请用英文文案",
                )
            except Exception as exc:  # noqa: BLE001
                rep.add("LCD1602 显示", FAIL, f"{type(exc).__name__}: {exc}", "先确认 i2cdetect 能看到转接板地址")

        buzzer = devices.get("alarm_buzzer")
        if buzzer is not None:
            try:
                from health_monitor.hal.models import BeepCommand

                print("\n>>> 注意听：蜂鸣器将响 2 声")
                buzzer.send(BeepCommand(times=2, on_ms=200, off_ms=200))
                ok = input("    听到 2 声蜂鸣了吗？[y/N] ").strip().lower().startswith("y")
                rep.add(
                    "蜂鸣器", PASS if ok else FAIL,
                    "用户确认听到蜂鸣" if ok else "用户报告没有声音",
                    "有源蜂鸣器 GPIO 直驱（GPIO18=物理脚12），串 220Ω~1kΩ 限流电阻\n"
                    "无源蜂鸣器需要 PWM 才响——本项目按**有源**实现，型号不对就换成有源的\n"
                    "确认负极接 GND、正极接 GPIO18",
                )
            except Exception as exc:  # noqa: BLE001
                rep.add("蜂鸣器", FAIL, f"{type(exc).__name__}: {exc}", "检查 GPIO 权限与引脚配置")

        speaker = devices.get("speaker")
        if speaker is not None:
            try:
                from health_monitor.hal.models import SpeakCommand

                print("\n>>> 注意听：蓝牙音箱应播报「测试完成」")
                speaker.send(SpeakCommand(text="设备测试完成，声音正常"))
                time.sleep(3.0)
                ok = input("    听到语音播报了吗？[y/N] ").strip().lower().startswith("y")
                rep.add(
                    "蓝牙音箱语音", PASS if ok else FAIL,
                    "用户确认听到播报" if ok else "用户报告没有声音或没配对",
                    "1) 检查工具：which espeak-ng aplay\n"
                    "2) 配对：bluetoothctl → pair <MAC> → trust <MAC> → connect <MAC>\n"
                    "3) 选输出：pactl set-default-sink bluez_sink.<MAC>.a2dp_sink\n"
                    "   或最小系统直接：aplay -D bluealsa:DEV=<MAC>,PROFILE=a2dp /usr/share/sounds/alsa/Front_Center.wav\n"
                    "4) 没配对成功也**不影响**其它功能：报警时蜂鸣器与 LED 仍会工作",
                )
            except Exception as exc:  # noqa: BLE001
                rep.add("蓝牙音箱语音", FAIL, f"{type(exc).__name__}: {exc}", "先确认 espeak-ng 与 aplay 已安装")

        sw = devices.get("sos_button")
        if sw is not None:
            print("\n>>> 现在**按下并松开**一次求救按钮")
            try:
                sw._require_open()  # noqa: SLF001 - 仅用于确认器件可用
                input("    按完回车继续...")
                events = []
                for _ in range(int(4 / 0.2)):
                    ev = sw.read()
                    if getattr(ev, "action", None) is not None and getattr(ev.action, "value", "") != "none":
                        events.append(ev.action.value)
                    time.sleep(0.2)
                if events:
                    rep.add("求救按键", PASS, f"检测到事件：{events}")
                else:
                    rep.add(
                        "求救按键", WARN, "没有检测到按键事件",
                        "按键一端 GPIO27（物理脚 13）、另一端 GND（物理脚 9），驱动默认启用内部上拉\n"
                        "按下时应读到低电平；若你的模块自带上拉/高电平输出，请在配置里设 pull_up=false",
                    )
            except Exception as exc:  # noqa: BLE001
                rep.add("求救按键", FAIL, f"{type(exc).__name__}: {exc}", "检查 GPIO 权限与引脚配置")

    # ---------------- 7.5 覆盖度提示（哪些器件本脚本还没数值判据） ----------------
    # 为什么要有这一段（2026-09-26）：加了 `--only` 之后，用户可以只验一个器件；
    # 但如果那个器件在本脚本里**没有专门的检查**（例如 tft 由 tft_check.py 验、
    # mcp3002 由 tmp36 组合使用），报告会只显示"打开成功"——看起来全绿，其实什么都没验。
    uncovered = [name for name in selected if name not in CHECKED_DEVICE_NAMES]
    if uncovered:
        hints = [
            f"  - {name}：{COMPOSED_DEVICE_HINTS.get(name, '本脚本还没有为它写数值判据')}"
            for name in uncovered
        ]
        rep.add(
            "检查覆盖", WARN,
            f"这些设备本次只做了\"能否打开\"检查（没有数值/输出判据）：{uncovered}",
            "\n".join(hints) + "\n（这不是失败，但报告里别把它们写成\"已实测通过\"）",
        )

    # ---------------- 8. 关闭设备 ----------------
    for name, dev in devices.items():
        try:
            dev.close()
        except Exception:  # noqa: BLE001
            pass

    code = rep.summary()
    if args.json:
        _write_json(args.json, rep)
    return code


def _write_json(path: str, rep: "Reporter") -> None:
    """把报告落盘（机器可读，便于留档与对比）。"""
    payload = {
        "ts": time.time(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "results": [r.as_dict() for r in rep.results],
        "counts": {s: sum(1 for r in rep.results if r.status == s) for s in (PASS, WARN, FAIL, SKIP)},
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入：{path}")


if __name__ == "__main__":
    raise SystemExit(main())
