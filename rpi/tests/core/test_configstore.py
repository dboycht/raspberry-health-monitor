"""``ConfigStore``（配置的读 / 校验 / 部分合并 / 原子落盘）测试。

为什么值得单独立一组：这一层是**唯一会改用户配置文件**的地方，
而配置文件写坏 = 服务再也起不来（``load_config`` 把 ConfigError 抛在启动路径上）。
所以每一条"拒绝写入"的判据都要有机器断言：
未知键、阈值不自洽、周期下限、GPIO 撞脚 —— 且**校验失败时文件一个字节都不动**。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_RPI_DIR = Path(__file__).resolve().parents[2]
if str(_RPI_DIR) not in sys.path:
    sys.path.insert(0, str(_RPI_DIR))

from health_monitor.core.config import ConfigError, Thresholds  # noqa: E402
from health_monitor.core.configstore import ConfigStore  # noqa: E402

#: 最小可用配置。刻意带上 `mqtt` / `onenet` 两个**面板不该碰**的顶层键，
#: 以及一个**未启用**的设备 —— 用来验证"只改认识的部分、其余原样保留"。
BASE = {
    "thresholds": {"hr_min": 50, "hr_max": 110, "spo2_min": 93},
    "devices": {
        "ambient": {"driver": "dht11", "read_interval_s": 3.0, "params": {"pin": 4}},
        "spo2_button": {"driver": "button", "read_interval_s": 0.2, "params": {"pin": 13}},
        "status_led": {
            "driver": "led", "read_interval_s": 1.0,
            "params": {"pins": {"green": 22, "yellow": 23, "red": 12}},
        },
        "tft": {
            "driver": "tft_spi", "enabled": False, "read_interval_s": 2.0,
            "params": {"dc_pin": 24, "reset_pin": 25},
        },
    },
    "mqtt": {"enabled": False, "host": "keep-me"},
    "onenet": {"enabled": False, "product_id": "keep-me-too"},
}


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "devices.json"
        self.path.write_text(json.dumps(BASE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # ⚠️ use_local=False：测试必须能**控制自己的环境**。
        # 否则断言会随"本机有没有 config/devices.local.json"而变（E56 那一类坑）。
        self.store = ConfigStore(self.path, use_local=False)

    def raw(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def bytes_on_disk(self) -> bytes:
        return self.path.read_bytes()


class TestSnapshot(_StoreCase):
    def test_快照含全部阈值字段(self) -> None:
        snap = self.store.snapshot()
        for name in Thresholds.__dataclass_fields__:
            self.assertIn(name, snap["thresholds"], f"面板看不到阈值字段 {name}")

    def test_快照列出每个设备的关键信息(self) -> None:
        devices = self.store.snapshot()["devices"]
        self.assertEqual(devices["ambient"]["driver"], "dht11")
        self.assertEqual(devices["ambient"]["read_interval_s"], 3.0)
        self.assertTrue(devices["ambient"]["enabled"])
        self.assertFalse(devices["tft"]["enabled"], "未启用的设备也要列出来（面板要能重新打开它）")

    def test_快照不返回引脚参数(self) -> None:
        """★ 用户明确要求"不做引脚号编辑"：快照里**不返回 params**，前端就没得渲染。"""
        for info in self.store.snapshot()["devices"].values():
            self.assertNotIn("params", info)


class TestPartialMerge(_StoreCase):
    def test_只改指定的那一项(self) -> None:
        result = self.store.apply_patch({"thresholds": {"spo2_min": 92}})
        self.assertEqual(result.changed, ["thresholds.spo2_min: 93 → 92"])
        self.assertEqual(result.raw["thresholds"]["spo2_min"], 92)
        self.assertEqual(result.raw["thresholds"]["hr_min"], 50, "没提到的阈值必须原样")

    def test_其它顶层键原样保留(self) -> None:
        """★ 面板绝不能把 `mqtt` / `onenet` 顺手抹掉（那会把密钥/上云配置弄丢）。"""
        result = self.store.apply_patch({"thresholds": {"spo2_min": 92}})
        self.assertEqual(result.raw["mqtt"], BASE["mqtt"])
        self.assertEqual(result.raw["onenet"], BASE["onenet"])

    def test_设备改动只落在指定设备上(self) -> None:
        result = self.store.apply_patch({"devices": {"ambient": {"enabled": False}}})
        self.assertFalse(result.raw["devices"]["ambient"]["enabled"])
        # 没提到的设备**一个字段都不该被写入**（"顺手补上默认值"也是在改别人的配置）
        other = result.raw["devices"]["spo2_button"]
        self.assertNotIn("enabled", other)
        self.assertEqual(other["read_interval_s"], 0.2)
        self.assertEqual(result.raw["devices"]["ambient"]["params"], {"pin": 4}, "参数不该被动过")

    def test_设备级的其他字段也不丢(self) -> None:
        result = self.store.apply_patch({"devices": {"ambient": {"enabled": False}}})
        self.assertEqual(result.raw["devices"]["ambient"]["driver"], "dht11")
        self.assertEqual(result.raw["devices"]["ambient"]["read_interval_s"], 3.0)

    def test_引脚参数不允许通过接口改(self) -> None:
        """★ 决定（2026-09-30）：**接口不提供改引脚的能力**。

        理由：① 用户选定的范围是"阈值 + 开关"（引脚那一项没选）；
        ② 用户同时选了"写操作不设防" —— 两者放一起意味着**同一 Wi-Fi 下任何人都能改硬件接线**，
        而"该响的不响"在现场极难定位。**"不设防"与"能改硬件"不能同时成立。**
        """
        with self.assertRaises(ConfigError) as ctx:
            self.store.apply_patch(
                {"devices": {"status_led": {"params": {"pins": {"red": 32}}}}}
            )
        self.assertIn("不支持的字段", str(ctx.exception))

    def test_没有变化时changed为空(self) -> None:
        result = self.store.apply_patch({"thresholds": {"spo2_min": 93}})
        self.assertEqual(result.changed, [], "值没变就不该报成'已改'")

    def test_把当前配置原样提交回来是彻底的no_op(self) -> None:
        """★ 面板"什么都没动就点保存"必须是 no-op。

        否则每保存一次都会给设备补上 `enabled: true` / `read_interval_s: 1.0`
        这类"其实等于默认值"的键 —— 文件被反复重写，备份文件还会一堆一堆地堆起来。
        判据分两层：既不能报 changed，也不能在内容上动一个键。
        """
        snap = self.store.snapshot()
        patch = {
            "thresholds": dict(snap["thresholds"]),
            "devices": {
                name: {"enabled": info["enabled"], "read_interval_s": info["read_interval_s"]}
                for name, info in snap["devices"].items()
            },
        }
        result = self.store.apply_patch(patch)
        self.assertEqual(result.changed, [], "原样提交不该报出任何改动")
        self.assertEqual(result.raw, BASE, "原样提交连'补默认键'都不该做")


class TestRejectsBadPatches(_StoreCase):
    def _should_reject(self, patch: dict, needle: str = "") -> str:
        before = self.bytes_on_disk()
        with self.assertRaises(ConfigError) as ctx:
            self.store.apply_patch(patch)
        self.assertEqual(self.bytes_on_disk(), before, "校验失败时文件不能被动过")
        message = str(ctx.exception)
        if needle:
            self.assertIn(needle, message)
        return message

    def test_未知顶层字段被拒绝(self) -> None:
        self._should_reject({"mqtt": {"enabled": True}}, "顶层字段")

    def test_未知阈值字段被拒绝(self) -> None:
        self._should_reject({"thresholds": {"spo2_minn": 92}}, "不认识的字段")

    def test_未知设备被拒绝(self) -> None:
        """拼错设备名不能静默新建一个空壳。"""
        self._should_reject({"devices": {"spoo2_button": {"enabled": False}}}, "没有设备")

    def test_未知设备字段被拒绝(self) -> None:
        self._should_reject({"devices": {"ambient": {"pin": 4}}}, "不支持的字段")

    def test_布尔值不能冒充数字(self) -> None:
        """`True` 在 Python 里是 `int` 的子类，会冒充 1 —— 必须显式拒绝。"""
        self._should_reject({"thresholds": {"hr_min": True}}, "必须是数字")

    def test_字符串阈值被拒绝(self) -> None:
        self._should_reject({"thresholds": {"hr_min": "五十"}}, "必须是数字")

    def test_阈值不自洽被拒绝(self) -> None:
        self._should_reject({"thresholds": {"hr_min": 120}}, "hr_min")

    def test_血氧下限越界被拒绝(self) -> None:
        self._should_reject({"thresholds": {"spo2_min": 0}})
        self._should_reject({"thresholds": {"spo2_min": 101}})

    def test_手工改出来的撞脚在保存时被拦下(self) -> None:
        """★ 撞脚检查在**写盘路径上仍然是活的**（不是装饰）。

        引脚只能手工改文件；但只要那份文件里存在撞脚，**通过面板保存任何东西**都必须被拦下来 ——
        否则面板会替一份坏配置"盖章"，而撞脚在真机上表现为
        "DHT11 一读，按键就被当成按下"这种**看起来毫不相干**的怪象。
        """
        raw = self.store.read_raw()
        raw["devices"]["spo2_button"]["params"]["pin"] = 4      # 与 DHT11 的 GPIO4 撞脚
        with self.assertRaises(ConfigError) as ctx:
            self.store.validate(raw)
        self.assertIn("撞脚", str(ctx.exception))
        self.assertIn("GPIO4", str(ctx.exception))

    def test_DHT11读取周期下限被拒绝(self) -> None:
        """DHT11 硬件要求两次读取间隔 ≥ 2 秒（`DeviceConfig.validate`）。"""
        self._should_reject({"devices": {"ambient": {"read_interval_s": 0.5}}}, "2 秒")

    def test_读取周期必须为正(self) -> None:
        self._should_reject({"devices": {"ambient": {"read_interval_s": 0}}})


class TestAtomicSave(_StoreCase):
    def test_保存带备份且不留临时文件(self) -> None:
        before = self.path.read_text(encoding="utf-8")
        result = self.store.apply_patch({"thresholds": {"spo2_min": 92}})
        backup = self.store.save(result.raw)

        self.assertTrue(backup, "改配置前必须先备份")
        self.assertTrue(Path(backup).exists())
        self.assertEqual(Path(backup).read_text(encoding="utf-8"), before,
                         "备份必须是**改动前**的原文，否则回滚没有意义")
        self.assertEqual(self.raw()["thresholds"]["spo2_min"], 92, "新值要落到盘上")
        self.assertEqual(list(self.path.parent.glob("*.tmp-*")), [],
                         "原子写不能留下临时文件（否则下次读可能读到半个 JSON）")

    def test_落盘后文件仍是合法JSON且能被重新加载(self) -> None:
        from health_monitor.core.config import load_config

        result = self.store.apply_patch({"thresholds": {"spo2_min": 92}, "devices": {"ambient": {"enabled": False}}})
        self.store.save(result.raw)
        reloaded = load_config(self.path, use_local=False)
        self.assertEqual(reloaded.thresholds.spo2_min, 92)
        self.assertIsNotNone(reloaded.device("ambient"))
        self.assertFalse(reloaded.device("ambient").enabled)  # type: ignore[union-attr]

    def test_同一秒内连存两次不覆盖上一份备份(self) -> None:
        first = self.store.save(self.store.apply_patch({"thresholds": {"spo2_min": 92}}).raw)
        second = self.store.save(self.store.apply_patch({"thresholds": {"spo2_min": 91}}).raw)
        self.assertNotEqual(first, second, "同一秒内的第二次保存不能压掉上一份回滚点")
        self.assertTrue(Path(first).exists() and Path(second).exists())

    def test_拒绝写入示例配置文件(self) -> None:
        """`devices.example.json` 是模板：写它等于把模板改成某台机器的配置。"""
        from health_monitor.core.config import example_config_path

        example = example_config_path()
        if not example.exists():
            self.skipTest("本机没有 devices.example.json")
        store = ConfigStore(example, use_local=False)
        with self.assertRaises(ConfigError) as ctx:
            store.save(BASE)
        self.assertIn("example", str(ctx.exception))


class TestSchemaGuards(_StoreCase):
    """两张表必须同步 —— 这类"漏登记一个字段"的洞肉眼查不出来。"""

    def test_可改字段都有对应的生效默认值(self) -> None:
        """`DEVICE_EFFECTIVE_DEFAULTS` 要覆盖 `DEVICE_PATCH_FIELDS` 的每一项。

        漏一个的后果：判断"到底改没改"时会拿一个哨兵值去比，
        于是**每次保存都报一次改动**、每次都写盘并多一个备份文件。
        """
        from health_monitor.core.configstore import (  # noqa: PLC0415
            DEVICE_EFFECTIVE_DEFAULTS,
            DEVICE_PATCH_FIELDS,
        )

        for field_name in DEVICE_PATCH_FIELDS:
            self.assertIn(field_name, DEVICE_EFFECTIVE_DEFAULTS, f"{field_name} 缺生效默认值")

    def test_可改字段都是配置文件里真实存在的键(self) -> None:
        """避免把 `DeviceConfig` 上不存在的名字写进白名单（那会静默接受无效改动）。"""
        from dataclasses import fields  # noqa: PLC0415

        from health_monitor.core.config import DeviceConfig  # noqa: PLC0415
        from health_monitor.core.configstore import DEVICE_PATCH_FIELDS  # noqa: PLC0415

        known = {f.name for f in fields(DeviceConfig)}
        for field_name in DEVICE_PATCH_FIELDS:
            self.assertIn(field_name, known, f"{field_name} 不是设备配置字段")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
