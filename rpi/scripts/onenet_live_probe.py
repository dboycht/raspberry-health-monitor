#!/usr/bin/env python3
"""OneNET 真联调探针：用**项目自己的驱动**连一次真实平台并上报一个数据点。

为什么要它
----------
"代码能跑"和"云上真能看到数据"是两件事。本脚本用项目里的
:class:`health_monitor.net.onenet.OneNetPublisher` 做一次真实连接 + 上报，
把结果（连接、订阅、平台回执 accepted/rejected）打出来——
这样上云这件事就有**实测证据**，而不是"文档说可以"。

用法（在**树莓派**上跑，参数从命令行给，**不写进仓库**）::

    cd ~/raspberry-health-monitor/rpi
    python3 scripts/onenet_live_probe.py \
        --pid <产品ID> --device <设备名> --key <设备密钥> \
        [--method sha1] [--wait 12] [--tls]

⚠️ 密钥是凭据：本脚本不打印它，也不落盘。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

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


def probe_paho() -> None:
    """先报告 paho 版本与可用的构造方式（新旧 API 差异会导致连接失败）。"""
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        safe_print(f"❌ 未安装 paho-mqtt：{exc}\n   → pip3 install paho-mqtt  或  sudo apt install -y python3-paho-mqtt")
        raise SystemExit(2)
    version = getattr(mqtt, "__version__", "未知")
    has_enum = hasattr(mqtt, "CallbackAPIVersion")
    print(f"paho-mqtt 版本：{version}；支持 CallbackAPIVersion：{'是' if has_enum else '否'}")
    for label, make in (
        ("旧式 Client(client_id)", lambda: mqtt.Client(client_id="compat-probe")),
        ("新式 Client(VERSION2, client_id)", lambda: mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "compat-probe")),
    ):
        try:
            make()
            safe_print(f"  ✅ {label} 可用")
        except Exception as exc:  # noqa: BLE001
            safe_print(f"  ❌ {label} 不可用：{type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="OneNET 真联调探针（只上报一个测试数据点）")
    parser.add_argument("--pid", required=True, help="产品 ID")
    parser.add_argument("--device", required=True, help="设备名称")
    parser.add_argument("--key", required=True, help="设备密钥（base64；不会被打出来）")
    parser.add_argument("--method", default="sha1", choices=["md5", "sha1", "sha256"])
    parser.add_argument("--wait", type=float, default=12.0, help="连上后等待回执的秒数")
    parser.add_argument("--tls", action="store_true", help="用 TLS（8883）")
    parser.add_argument("--stream", default="probe_temp", help="测试用数据流名")
    args = parser.parse_args()

    print("=" * 78)
    print("OneNET 真联调探针")
    print("=" * 78)

    print("\n【第一步】paho 环境")
    probe_paho()

    from health_monitor.net.onenet import OneNetConfig, OneNetPublisher, device_resource

    cfg = OneNetConfig(
        enabled=True,
        product_id=args.pid,
        device_name=args.device,
        access_key=args.key,
        method=args.method,
        tls=bool(args.tls),
        keepalive=120,
        interval_s=1.0,
        subscribe_result=True,
    )
    print("\n【第二步】配置与 token")
    print(f"  res     = {device_resource(cfg.product_id, cfg.device_name)}")
    print(f"  method  = {cfg.method}")
    token = cfg.make_token()
    print(f"  token   = 已生成（长度 {len(token)}，内容不打印）")

    print("\n【第三步】连接并上报一个数据点")
    pub = OneNetPublisher(cfg)
    started = pub.start()
    print(f"  start() -> {started}" + (f"（原因：{pub.reason}）" if not started else ""))
    if not started:
        return 1

    # 等连接建立
    deadline = time.time() + 10
    while time.time() < deadline and not pub.connected:
        time.sleep(0.2)
    print(f"  connected = {pub.connected}")

    payload_preview = OneNetPublisher.build_datapoint(1, {args.stream: 25.0}, ts=time.time())
    print(f"  将上报：{json.dumps(payload_preview, ensure_ascii=False)}")
    pub.publish_reading({args.stream: 25.0}, ts=time.time())

    print(f"\n【第四步】等待平台回执（最多 {args.wait:g} 秒）—— 这是「上没上去」的唯一硬证据")
    deadline = time.time() + args.wait
    while time.time() < deadline and not pub.received_results:
        time.sleep(0.3)

    status = pub.status()
    print(f"  平台回执条数：{len(pub.received_results)}")
    for item in pub.received_results[-3:]:
        print(f"   - {item['topic']}  {json.dumps(item['payload'], ensure_ascii=False)[:160]}")
    print(f"  published={status['published']} failed={status['failed']} dropped={status['dropped']}")
    if status.get("last_error"):
        print(f"  last_error={status['last_error']}")

    print("\n【结论】")
    if pub.received_results and any("accepted" in item["topic"] for item in pub.received_results):
        safe_print("  🎉 平台已接收数据点（accepted）—— 上云链路**实测打通**")
        code = 0
    elif pub.connected and status["published"]:
        safe_print("  ⚠️ 已连接且已发布，但没等到 accepted 回执："
              "可能是平台不推送回执，或订阅未生效；请到控制台看该设备的数据流是否出现 "
              f"{args.stream}")
        code = 0
    else:
        safe_print("  ❌ 未打通：看上面的 connected / published / last_error 三项定位")
        code = 1

    pub.stop()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
