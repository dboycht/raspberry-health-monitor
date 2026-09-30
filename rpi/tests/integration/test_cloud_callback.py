"""`POST /api/v1/cloud/callback` 的测试 —— **2026-10-01 之前它一行测试都没有**。

## 为什么此前没有测试，以及那导致了什么

这个 handler 从写出来的那一天（OneNET 上云那一轮）起**就没有注册进路由表** ——
既不在老的 `if/elif` 链里、也不在后来改成的路由字典里。它是**死代码**，
而 `CHANGELOG-接口.md` 与手册都写着这个端点存在。

实测（回归修复前）：

    POST /api/v1/cloud/callback  ->  HTTP 404
    GET  /api/v1/health          ->  HTTP 200   （对照）

⇒ **OneNET 规则引擎推送这条路从来没通过**。`docs/07` 的 C7"规则引擎转发"长期写着"未实测"，
真相是**根本没法测** —— 端点不存在。

**为什么几个月没人发现**：项目对"报警码"有"三方一致"守卫，却**没有任何守卫比较
"路由表 ↔ 契约文档"**。本轮补上了：
`tests/integration/test_api_contract_docs.py`（双向比对，它当场抓到了这条）。

本文件补的是**行为**测试 —— 尤其是最后那条设计意图：**回调只记录，绝不驱动报警**
（否则"自己上报的数据被平台转发回来"会形成自激回路）。
"""

from __future__ import annotations

import json
import unittest

from health_monitor.core.config import AppConfig
from health_monitor.net.web import WebApi
from health_monitor.playback import PlaybackRuntime

DEMO = {
    "thresholds": {"no_motion_timeout_s": 60, "repeat_cooldown_s": 0},
    "devices": {
        "vitals": {"driver": "max30102", "read_interval_s": 1.0},
        "ambient": {"driver": "dht11", "read_interval_s": 3.0},
        "status_led": {"driver": "led", "read_interval_s": 1.0},
    },
}

PATH = "/api/v1/cloud/callback"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.rt = PlaybackRuntime(config=AppConfig.from_dict(DEMO), clock=self.clock)
        self.rt.open()
        self.addCleanup(self.rt.close)

    def _post(self, body: bytes, query=None):
        api = WebApi(self.rt)
        return api.handle("POST", PATH, query or {}, {}, body=body)

    def _post_json(self, payload, query=None):
        return self._post(json.dumps(payload, ensure_ascii=False).encode("utf-8"), query)


class _Clock:
    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class TestCloudCallbackIsWired(_Base):
    def test_路由表里真的注册了它(self) -> None:
        """★ 这条就是那次回归的钉子：**文档里有的端点，必须真的能调到**。

        回归前这里返回 404（`未知接口 POST ...`），而 handler 一直躺在文件里。
        """
        code, body = self._post_json({"ts": 1, "payload": {"x": 1}})
        self.assertNotEqual(code, 404, f"端点又变成没注册了：{body}")
        self.assertEqual(code, 200)

    def test_返回平台约定的成功应答(self) -> None:
        """OneNET 规则引擎按这个约定判成功，格式改了就收不到重试/回执。"""
        code, body = self._post_json({"ts": 1, "payload": {"heart_rate": 70}})
        self.assertEqual(code, 200)
        self.assertEqual(body, {"code": 0, "msg": "ok"})


class TestCloudCallbackRecords(_Base):
    def test_推送被记录下来(self) -> None:
        payload = {"ts": 1, "payload": {"heart_rate": 70, "spo2": 97}}
        self._post_json(payload)
        pushes = self.rt.recent_cloud_pushes()
        self.assertEqual(len(pushes), 1)
        # ⚠️ 存的是**整份推送报文**（不拆里面的 `payload` 键）—— 这是"留证"语义：
        #    "云端到底发了什么"要原样保留，拆过就不算原始证据了。
        #    （第一版我把期望写成了内层字典，测试当场把这个差异摆出来了。）
        self.assertEqual(pushes[0]["payload"], payload)
        self.assertEqual(self.rt.cloud_push_count, 1)

    def test_不是JSON也要留证(self) -> None:
        """"云端到底发了什么"是排查云云对接的唯一硬证据 —— 解析不了也必须留下来。"""
        self._post(b"this is not json")
        pushes = self.rt.recent_cloud_pushes()
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0]["payload"], "this is not json")

    def test_query参数也记下来(self) -> None:
        self._post_json({"a": 1}, query={"token": ["abc"], "id": ["7"]})
        self.assertEqual(self.rt.recent_cloud_pushes()[0]["query"], {"token": "abc", "id": "7"})

    def test_空body也不崩(self) -> None:
        code, body = self._post(b"")
        self.assertEqual(code, 200)
        self.assertEqual(body, {"code": 0, "msg": "ok"})


class TestCloudCallbackDoesNotDriveAlarms(_Base):
    """★ 这个端点存在的**理由**：云云对接不能形成自激回路。

    OneNET 规则引擎会把我们**自己刚上报**的数据按规则转发回来。若这条回调去驱动报警判定，
    那么"上报 → 平台转发回来 → 又触发报警 → 再上报"就是一个正反馈环，
    而且平台一次重发就能凭空造出一条报警。
    """

    def test_看起来像报警的推送也不改变任何监护状态(self) -> None:
        self.rt.tick()
        before = self.rt.engine.active_alarms()
        # 故意塞一份"看起来会触发血氧过低"的数据
        self._post_json({"payload": {"heart_rate": 200, "spo2": 80, "finger_detected": True}})
        self.rt.tick()
        self.assertEqual(self.rt.engine.active_alarms(), before,
                         "回调**只记录**，绝不许驱动报警判定")

    def test_回调不进报警流水(self) -> None:
        self._post_json({"payload": {"heart_rate": 200, "spo2": 80}})
        codes = [getattr(e.code, "value", "") for e in self.rt.recent_events(limit=50)]
        self.assertNotIn("hr_too_high", codes)
        self.assertNotIn("spo2_too_low", codes)


class TestCloudLast(_Base):
    """`GET /api/v1/cloud/last` —— 把 `_cloud_callback` 记下的原件读出来。

    它此前**只在 docstring 里被承诺过、从未实现**（2026-10-01 发现），
    于是"推送被记进内存却没有任何办法读出来" ⇒ 自称的"唯一硬证据"等于不存在。
    """

    def _get(self, query=None):
        api = WebApi(self.rt)
        return api.handle("GET", "/api/v1/cloud/last", query or {}, {})

    def test_没收到过推送时是空的(self) -> None:
        code, body = self._get()
        self.assertEqual(code, 200)
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["pushes"], [])
        self.assertEqual(body["total"], 0)

    def test_能读出收到的推送_按时间正序(self) -> None:
        self._post_json({"payload": {"n": 1}})
        self._post_json({"payload": {"n": 2}})
        code, body = self._get()
        self.assertEqual(code, 200)
        self.assertEqual(body["count"], 2)
        self.assertEqual([p["payload"]["payload"]["n"] for p in body["pushes"]], [1, 2],
                         "新的在后（与既有历史类接口同一口径）")
        self.assertEqual(body["total"], 2)

    def test_limit生效但total仍是总数(self) -> None:
        for i in range(5):
            self._post_json({"payload": {"n": i}})
        _code, body = self._get({"limit": ["2"]})
        self.assertEqual(body["count"], 2, "limit 只截取返回条数")
        self.assertEqual(body["total"], 5, "total 是累计收到过多少条，不受 limit 影响")

    def test_不是JSON的推送也读得到(self) -> None:
        self._post(b"garbage-not-json")
        _code, body = self._get()
        self.assertEqual(body["pushes"][0]["payload"], "garbage-not-json")


class TestCloudCallbackRespectsToken(_Base):
    def test_设了token且不带就401(self) -> None:
        """`--token` 是整体开关：**所有**接口都受它管，回调也不能例外。"""
        api = WebApi(self.rt, token="s3cret")
        code, body = api.handle("POST", PATH, {}, {}, body=b'{"a":1}')
        self.assertEqual(code, 401)
        self.assertFalse(body.get("ok", True))
        self.assertEqual(self.rt.recent_cloud_pushes(), [], "401 之后不该留记录")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
