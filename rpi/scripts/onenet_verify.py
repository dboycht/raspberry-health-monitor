#!/usr/bin/env python3
"""OneNET 端到端验收（在**树莓派**上跑，读项目配置，不接任何硬件）。

它证明的是**配置链路**这条最容易出错、又最难自查的路：

    devices.json（产品ID/设备名/topic） + devices.local.json（密钥）
        → load_config() 深度合并
        → OneNetConfig.from_dict() 校验
        → token 生成（sha1/sha256…）
        → MQTT 连接 → 数据点上报 → **平台回执 accepted**

用法（在树莓派上）::

    cd ~/raspberry-health-monitor/rpi
    python3 scripts/onenet_verify.py            # 用项目配置里的真参数
    python3 scripts/onenet_verify.py --dry-run  # 只打印配置与 token 长度，不连网

⚠️ 本脚本**不打印密钥**，只打印长度；也不把任何凭据写入文件。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

RPI_DIR = Path(__file__).resolve().parents[1]
if str(RPI_DIR) not in sys.path:
    sys.path.insert(0, str(RPI_DIR))


def main() -> int:
    parser = argparse.ArgumentParser(description="OneNET 端到端验收（配置 → token → 上报 → 回执）")
    parser.add_argument("--dry-run", action="store_true", help="只检查配置与 token，不连接平台")
    parser.add_argument("--wait", type=float, default=12.0, help="等回执的秒数")
    args = parser.parse_args()

    print("=" * 78)
    print("OneNET 端到端验收")
    print("=" * 78)

    from health_monitor.core.config import load_config, local_config_path
    from health_monitor.net.onenet import OneNetConfig, OneNetPublisher, device_resource

    # --- 第 1 步：配置合并 ---
    local = local_config_path()
    print("\n【1】配置来源")
    print(f"  基础配置：devices.json")
    print(f"  本机覆盖：{local}　{'存在' if local.exists() else '不存在（密钥只能靠环境变量）'}")

    app = load_config()
    raw = dict(app.onenet or {})
    print("\n【2】合并后的 onenet 段（密钥只报长度）")
    for key in ("enabled", "platform", "product_id", "device_name", "method", "host", "port",
                "tls", "keepalive", "interval_s", "topic_template", "subscribe_result"):
        if key in raw:
            print(f"  {key:<16} = {raw[key]}")
    key_len = len(str(raw.get("access_key") or ""))
    env_name = raw.get("key_env", "HEALTH_ONENET_KEY")
    import os
    env_len = len(os.environ.get(env_name, ""))
    print(f"  access_key       长度 = {key_len}")
    print(f"  {env_name} 环境变量长度 = {env_len}")

    cfg = OneNetConfig.from_dict(raw)
    if cfg.resolved_key():
        print("  ✅ 密钥已解析到（来自配置文件或环境变量）")
    else:
        print(f"  ❌ 没有可用密钥：设 {env_name} 或在 devices.local.json 里写 access_key")
        return 2

    # --- 第 3 步：token ---
    print("\n【3】连接参数")
    print(f"  res    = {device_resource(cfg.product_id, cfg.device_name)}")
    print(f"  host   = {cfg.resolved_host()}:{cfg.resolved_port()}（TLS={cfg.tls}）")
    print(f"  client = {cfg.device_name}　username = {cfg.product_id}")
    token = cfg.make_token()
    print(f"  token  = 已生成，长度 {len(token)}（内容不打印）")

    if args.dry_run:
        print("\n【4】--dry-run：跳过真实连接")
        return 0

    # --- 第 4 步：真连真发 ---
    print("\n【4】连接平台并上报一个数据点（真实写入，用于验收）")
    pub = OneNetPublisher(cfg)
    if not pub.start():
        print(f"  ❌ 启动失败：{pub.reason}")
        return 1
    deadline = time.time() + 10
    while time.time() < deadline and not pub.connected:
        time.sleep(0.2)
    print(f"  connected = {pub.connected}")
    if not pub.connected:
        print(f"  ❌ 连不上：{pub.reason}")
        pub.stop()
        return 1

    # 用**真实映射键**（stream_map 里的本地量名）——用自造键会被忽略且只记 warning
    summary = {
        "ts": time.time(),
        "heart_rate_bpm": 72.0,
        "spo2_percent": 98.0,
        "body_temp_c": 36.5,
        "ambient_temp_c": 24.8,
        "humidity_percent": 56.0,
        "motion_state": "detected",
        "data_age_s": 1.5,
        "data_stale": False,
    }
    pub.publish_reading(summary, ts=time.time())
    print(f"  入队后队列长度 = {pub._queue.qsize()}")

    print(f"\n【5】等待平台回执（最多 {args.wait:g} 秒）")
    deadline = time.time() + args.wait
    while time.time() < deadline and not pub.received_results:
        time.sleep(0.3)

    status = pub.status()
    accepted = [r for r in pub.received_results if "accepted" in r["topic"]]
    rejected = [r for r in pub.received_results if "rejected" in r["topic"]]
    print(f"  published={status['published']} failed={status['failed']} "
          f"skipped_empty={status['skipped_empty']}")
    for item in pub.received_results[-3:]:
        print(f"  回执：{item['topic']}")
        print(f"        {json.dumps(item['payload'], ensure_ascii=False)[:200]}")

    print("\n【结论】")
    if accepted:
        print("  🎉 平台已接收（accepted）—— OneNET 链路**实测打通**")
        print("     去控制台看：设备 t1 → 数据流，应能看到 heart_rate_bpm / spo2_percent / ... ")
        code = 0
    elif rejected:
        print("  ❌ 平台拒绝（rejected）：常见原因是数据流未创建（旧版无需创建）、"
              "token 过期、或 payload 里有非法值")
        code = 1
    else:
        print("  ⚠️ 已发布但没等到回执；请到控制台确认数据流是否更新")
        code = 0
    pub.stop()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
