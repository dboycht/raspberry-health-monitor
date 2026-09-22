#!/usr/bin/env python3
"""定位 OneNET 上报链路：连接成功但 published=0 的原因。

排查思路（分层）：
1. paho 的**回调签名**是否匹配（paho 2.x 默认 CallbackAPIVersion 是 VERSION1 还是 VERSION2？
   两者 `on_connect` 的参数个数不同，签名不对会导致回调抛异常但连接仍显示成功）；
2. 数据点是否真的**进了队列**（入队条件、payload 是否为空）；
3. 工作线程有没有**取到消息并调用 publish**（publish 的 rc 是多少）。

用法（在树莓派上）::

    cd ~/raspberry-health-monitor/rpi
    python3 scripts/diag_onenet_publish.py --pid <pid> --device <name> --key <key>
"""

from __future__ import annotations

import argparse
import inspect
import sys
import time
from pathlib import Path

RPI_DIR = Path(__file__).resolve().parents[1]
if str(RPI_DIR) not in sys.path:
    sys.path.insert(0, str(RPI_DIR))

import paho.mqtt.client as mqtt  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="诊断 OneNET published=0")
    parser.add_argument("--pid", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--method", default="sha1")
    parser.add_argument("--wait", type=float, default=8.0)
    args = parser.parse_args()

    print("=" * 78)
    print("诊断：OneNET 连接成功但 published=0")
    print("=" * 78)

    print("\n【1】paho 回调签名探针（关键）")
    print(f"  paho 模块：{mqtt.__file__}")
    for label, factory in (
        ("VERSION1 显式", lambda: mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "t1")),
        ("VERSION2 显式", lambda: mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "t1")),
        ("旧式（默认）", lambda: mqtt.Client(client_id="t1")),
    ):
        try:
            c = factory()
            api = getattr(c, "_callback_api_version", None)
            on_connect = getattr(c, "on_connect", None)
            # 看回调被包装后的可调用签名（paho 会按 API 版本包装）
            try:
                sig = str(inspect.signature(on_connect)) if on_connect else "无"
            except Exception as exc:  # noqa: BLE001
                sig = f"<无法读取: {exc}>"
            print(f"  {label:<14} 构造 OK；_callback_api_version={api}；on_connect 签名={sig}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:<14} ❌ {type(exc).__name__}: {exc}")

    print("\n【2】用项目驱动连接并手动观察队列/线程")
    from health_monitor.net.onenet import OneNetConfig, OneNetPublisher

    cfg = OneNetConfig(
        enabled=True, product_id=args.pid, device_name=args.device,
        access_key=args.key, method=args.method, interval_s=1.0, subscribe_result=True,
    )
    pub = OneNetPublisher(cfg)
    print(f"  start() -> {pub.start()}")
    deadline = time.time() + 8
    while time.time() < deadline and not pub.connected:
        time.sleep(0.2)
    print(f"  connected = {pub.connected}")
    print(f"  队列大小（入队前）= {pub._queue.qsize()}")
    print(f"  _client 是否为 None = {pub._client is None}")

    payload = OneNetPublisher.build_datapoint(1, {"probe_temp": 25.0}, ts=time.time())
    print(f"  准备上报的 payload = {payload}")
    pub.publish_reading({"probe_temp": 25.0}, ts=time.time())
    print(f"  队列大小（入队后）= {pub._queue.qsize()}  ← 0 表示**根本没入队**")

    time.sleep(max(1.0, args.wait))
    st = pub.status()
    print(f"  published={st['published']} failed={st['failed']} dropped={st['dropped']}")
    print(f"  last_error={st.get('last_error')}")
    print(f"  回执={pub.received_results[-2:] if pub.received_results else '（无）'}")

    print("\n【3】绕开项目驱动，直接用 paho 发一次（对照组）")
    token = cfg.make_token()
    ok_direct = False
    for api_label, api_ver in (("VERSION2", mqtt.CallbackAPIVersion.VERSION2), ("VERSION1", mqtt.CallbackAPIVersion.VERSION1)):
        try:
            c = mqtt.Client(api_ver, args.device)
        except Exception as exc:  # noqa: BLE001
            print(f"  {api_label}: 构造失败 {exc}")
            continue
        seen = {"connected": False, "rc": None}
        if api_ver is mqtt.CallbackAPIVersion.VERSION2:
            def on_connect(client, userdata, flags, reason_code, properties=None):
                seen["connected"] = not reason_code.is_failure
        else:
            def on_connect(client, userdata, flags, rc):
                seen["connected"] = (rc == 0)
        c.on_connect = on_connect
        c.username_pw_set(args.pid, token)
        try:
            c.connect(cfg.resolved_host(), cfg.resolved_port(), 120)
            c.loop_start()
            t0 = time.time()
            while time.time() - t0 < 5 and not seen["connected"]:
                time.sleep(0.1)
            if seen["connected"]:
                info = c.publish(f"$sys/{args.pid}/{args.device}/dp/post/json",
                                 '{"id":1,"dp":{"probe_temp":[{"v":25.0}]}}', qos=1)
                info.wait_for_publish(timeout=5)
                print(f"  {api_label}: connected=True；publish rc={info.rc}；is_published={info.is_published()}")
                ok_direct = True
            else:
                print(f"  {api_label}: 未连上（5 秒内）")
        except Exception as exc:  # noqa: BLE001
            print(f"  {api_label}: 异常 {type(exc).__name__}: {exc}")
        finally:
            try:
                c.loop_stop(); c.disconnect()
            except Exception:  # noqa: BLE001
                pass

    print("\n【4】结论")
    if ok_direct:
        print("  ✅ 直接用 paho 能发出（对照组成功）⇒ 问题在**项目驱动的回调/发布路径**")
    else:
        print("  ⚠️ 对照组也没发出去 ⇒ 先看平台侧权限/主题，再怀疑代码")
    pub.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
