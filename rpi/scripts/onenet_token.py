#!/usr/bin/env python3
"""OneNET token 生成 / 自检工具（照着填控制台或配置文件即可）。

背景（口径来自 OneNET 官方文档，详见 ``health_monitor/net/onenet.py`` 顶部注释）
------------------------------------------------------------------------------
旧版「MQTT物联网套件 / 多协议接入」的设备连接三要素是：

    clientId = 设备名称
    username = 产品ID
    password = token（用**设备密钥**按 res=products/{产品ID}/devices/{设备名} 算出来的）

token 算法：``version=2018-10-31&res=<res>&et=<过期unix秒>&method=<md5|sha1|sha256>&sign=<...>``，
``sign = base64(hmac_<method>(base64decode(access_key), "et\\nmethod\\nres\\nversion"))``，
其中 ``res`` 与 ``sign`` 作为 value 要 URL 编码。

用法
----
    cd rpi
    python scripts/onenet_token.py --pid 123456 --device living-room-pi --key "<设备密钥>"
    python scripts/onenet_token.py --pid 123456 --device living-room-pi --key-env HEALTH_ONENET_KEY
    python scripts/onenet_token.py --pid 123456 --device x --key "<key>" --ttl 86400 --method sha256
    python scripts/onenet_token.py --selftest        # 用官方文档给的样例密钥自检算法实现

⚠️ token 是凭据：本脚本**打印 token 是刻意的**（你要复制到控制台/调试工具里用），
但请不要把 key 与 token 提交进仓库、也不要贴到公共群里。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

RPI_DIR = Path(__file__).resolve().parents[1]
if str(RPI_DIR) not in sys.path:
    sys.path.insert(0, str(RPI_DIR))

from health_monitor.net.onenet import (  # noqa: E402
    DEFAULT_HOST_PLAIN,
    DEFAULT_HOST_TLS,
    DEFAULT_PORT_PLAIN,
    DEFAULT_PORT_TLS,
    PLATFORM_LEGACY,
    PLATFORM_STUDIO,
    OneNetError,
    device_resource,
    product_resource,
    sign_token,
)


def selftest() -> int:
    """用**可复算的已知值**自检算法实现（不联网、不依赖平台）。

    自检方式：自己按官方公式手算一遍，与 :func:`sign_token` 的结果逐字符比对。
    这样即使将来有人"优化"了算法实现，这里会立刻红。
    """
    key_b64 = "KuF3NT/jUBJ62LNBB/A8XZA9CqS3Cu79B/ABmfA1UCw="
    res = "products/123123"
    et = 1537255523
    version = "2018-10-31"

    problems = []
    for method in ("md5", "sha1", "sha256"):
        # 手算（照官方文档的公式，独立于被测实现）
        org = f"{et}\n{method}\n{res}\n{version}"
        digest = hmac.new(base64.b64decode(key_b64), org.encode(), digestmod=method).digest()
        expect_sign = base64.b64encode(digest).decode()
        token = sign_token(key_b64, res, et=et, method=method)
        if f"et={et}" not in token or f"method={method}" not in token:
            problems.append(f"{method}: token 缺少 et/method 字段")
        if f"version={version}" not in token:
            problems.append(f"{method}: token 缺少 version 字段")
        # token 里的 sign 是 URL 编码过的，解码回来应与手算一致
        import urllib.parse

        supplied = dict(part.split("=", 1) for part in token.split("&"))
        got_sign = urllib.parse.unquote(supplied.get("sign", ""))
        if got_sign != expect_sign:
            problems.append(f"{method}: sign 不一致\n  期望 {expect_sign}\n  实际 {got_sign}")
        got_res = urllib.parse.unquote(supplied.get("res", ""))
        if got_res != res:
            problems.append(f"{method}: res 编码/解码后不一致：{got_res} != {res}")

    # 边界：非法 method / 空 key / 空 res 都要**明确报错**（不许静默生成坏 token）
    for kwargs, label in (
        ({"method": "sha512"}, "不支持的 method"),
        ({"access_key": ""}, "空 key"),
        ({"res": ""}, "空 res"),
    ):
        args = {"access_key": key_b64, "res": res, "et": et, "method": "sha1"}
        args.update(kwargs)
        try:
            sign_token(**args)
        except OneNetError:
            pass
        else:
            problems.append(f"边界检查失败：{label} 竟然没有报错")

    if problems:
        print("❌ 自检失败：")
        for p in problems:
            print("   - " + p)
        return 1
    print("✅ 自检通过：md5 / sha1 / sha256 三种签名的手算结果与实现一致；非法入参会明确报错")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OneNET token 生成与自检")
    parser.add_argument("--pid", default="", help="产品 ID（控制台里那串数字）")
    parser.add_argument("--device", default="", help="设备名称（同一产品内唯一）")
    parser.add_argument("--key", default="", help="设备密钥（base64 字符串）")
    parser.add_argument("--key-env", default="HEALTH_ONENET_KEY",
                        help="从该环境变量读密钥（默认 HEALTH_ONENET_KEY）")
    parser.add_argument("--method", default="sha256", choices=["md5", "sha1", "sha256"])
    parser.add_argument("--ttl", type=int, default=86400, help="有效期秒数（默认 86400=1 天）")
    parser.add_argument("--product-level", action="store_true",
                        help="生成产品级 token（res=products/{pid}，用于调用平台 API，不能用于设备连接）")
    parser.add_argument("--platform", default=PLATFORM_LEGACY, choices=[PLATFORM_LEGACY, PLATFORM_STUDIO],
                        help="产品线：legacy=旧版 MQTT物联网套件（数据流-数据点，本项目用的）；"
                             "studio=OneNET Studio（物模型，本适配器不支持，仅生成 token 供参考）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（便于脚本消费）")
    parser.add_argument("--selftest", action="store_true", help="只跑算法自检")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    if args.platform == PLATFORM_STUDIO:
        print("⚠️ 你选了 studio（OneNET Studio，物模型 OneJSON）。")
        print("   本项目的适配器只支持旧版「MQTT物联网套件」（数据流-数据点），")
        print("   两者**鉴权算法相同、上报 topic 与 payload 不同**：")
        print("     - legacy：$sys/{pid}/{device}/dp/post/json，报文 {id, dp:{流:[{v,t}]}}")
        print("     - studio：物模型属性/事件/服务主题，报文 params.xxx.value（OneJSON）")
        print("   下面仍然按同样的算法生成 token（算法两套通用），但**上报格式需要另写适配器**。")
        print("   判断自己在哪套平台：建产品时有没有让你'定义物模型/属性' —— 有=studio。")
        print("-" * 78)

    key = args.key or os.environ.get(args.key_env, "")
    if not key:
        print("❌ 没拿到密钥：请用 --key 或设置环境变量 " + args.key_env, file=sys.stderr)
        return 2
    if not args.pid:
        print("❌ 缺少 --pid（产品 ID）", file=sys.stderr)
        return 2
    if not args.product_level and not args.device:
        print("❌ 缺少 --device（设备名称）；若确实要产品级 token 请加 --product-level", file=sys.stderr)
        return 2

    res = product_resource(args.pid) if args.product_level else device_resource(args.pid, args.device)
    try:
        token = sign_token(key, res, method=args.method, ttl_s=args.ttl)
    except OneNetError as exc:
        print(f"❌ 生成失败：{exc}", file=sys.stderr)
        return 1

    now = int(time.time())
    result = {
        "platform": args.platform,
        "platform_note": (
            "旧版 MQTT物联网套件（数据流-数据点）—— 本项目支持"
            if args.platform == PLATFORM_LEGACY
            else "OneNET Studio（物模型 OneJSON）—— 本项目**不支持**上报格式，需另写适配器"
        ),
        "host": DEFAULT_HOST_PLAIN,
        "port": DEFAULT_PORT_PLAIN,
        "tls_host": DEFAULT_HOST_TLS,
        "tls_port": DEFAULT_PORT_TLS,
        "client_id": args.device or "(产品级，不用于设备连接)",
        "username": args.pid,
        "password_token": token,
        "res": res,
        "method": args.method,
        "issued_at": now,
        "expires_at": now + args.ttl,
        "expires_local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + args.ttl)),
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print("=" * 78)
    print("OneNET（旧版 MQTT物联网套件）设备连接参数")
    print("=" * 78)
    print(f"  服务地址（非加密）：{result['host']} : {result['port']}")
    print(f"  服务地址（TLS）   ：{result['tls_host']} : {result['tls_port']}（需要证书，见官方文档）")
    print(f"  clientId（=设备名）：{result['client_id']}")
    print(f"  username（=产品ID）：{result['username']}")
    print(f"  password（=token） ：{token}")
    print("-" * 78)
    print(f"  res     ：{res}")
    print(f"  method  ：{args.method}")
    print(f"  有效期至：{result['expires_local']}（{args.ttl} 秒）")
    print("-" * 78)
    print("提醒：")
    print("  1) 设备连接**必须用设备级密钥**（res=products/{pid}/devices/{设备名}）；")
    print("     产品级 token 只能调平台 API，拿去连接会被拒（rc=5 未授权）。")
    print("  2) keepalive 允许 10~1800 秒；平台在 1.5×keepalive 内没收到上行数据会断开。")
    print("  3) token 会过期：本项目在启动时现签，有效期由 config 的 token_ttl_s 控制。")
    print("  4) 把上面的参数填到 rpi/config/devices.json 的 onenet 段，或设环境变量")
    print("     HEALTH_ONENET_KEY=<设备密钥>，然后把 onenet.enabled 改成 true。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
