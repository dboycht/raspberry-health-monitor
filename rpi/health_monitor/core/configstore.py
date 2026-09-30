"""配置的**读 / 校验 / 部分合并 / 原子落盘**（Web 配置面板的后端，也可被 CLI 复用）。

硬约束（每条都有理由）
----------------------
1. **读原始 JSON、只改认识的部分**
   ``config/devices.json`` 里可能有我们不建模的键（``mqtt``/``onenet`` 的细节、以后新增的字段）。
   面板改阈值/开关时**绝不能顺手把它们抹掉** —— 所以走"读原始 dict → 就地改 → 写回"，
   而**不是**"把 :class:`AppConfig` 序列化回去"：后者会丢掉所有没建模的字段，
   而且会把 ``devices.local.json`` 的**合并结果**写进基础文件 ——
   那等于**把密钥写进仓库**。这是本项目最不能犯的错之一。
2. **写盘之前必须校验**：:meth:`AppConfig.from_dict`（含 ``Thresholds.validate()`` 与
   "未知键一律报错"）+ **撞脚检查**。校验不过就抛 :class:`ConfigError`，
   **一个字节都不写**（测试里有"文件未被改动"的断言钉住）。
3. **原子写**：临时文件 → ``fsync`` → :func:`os.replace`。树莓派被拔电是常态，
   写一半的 JSON 会让服务**再也起不来**（``load_config`` 把 ConfigError 抛在启动路径上）。
4. **写前备份**：``devices.json.bak-<YYYYmmdd-HHMMSS>``，出问题可以人工回滚。
5. **本机覆盖参与校验、但不参与写盘**：真正跑起来的是"基础配置 + ``devices.local.json``"，
   所以校验的是**合并后**的结果（否则面板能改出一份"单独看合法、合起来非法"的配置）；
   但只写基础文件。

⚠️ 与 `docs` 的分工：本模块只负责"把配置安全地改掉"，**不负责热应用**；
热应用在 :meth:`health_monitor.service.Runtime.apply_config`（改完要让**正在跑的**
进程当场换掉阈值/周期/器件开关）。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from .config import (
    AppConfig,
    ConfigError,
    Thresholds,
    example_config_path,
    merge_override,
    read_local_overrides,
)
from .pincheck import find_config_conflicts

_LOG = logging.getLogger(__name__)

#: 设备级**允许**通过配置接口修改的字段。
#:
#: ⚠️ **刻意不含 `params`（引脚与驱动参数）**，理由两条（2026-09-30 定的）：
#:
#: 1. **用户选定的范围就是"阈值 + 开关"** —— 选项里单列过"阈值 + 开关 + 引脚等底层项"，
#:    用户**没有**选它。既然面板"不做引脚号编辑"，就不该在接口上留一个侧门。
#: 2. **它与"写操作不设防"直接冲突**：用户选了"局域网内不鉴权就能改配置"（演示方便）。
#:    若接口还能改引脚，**同一个 Wi-Fi 下任何人都能把蜂鸣器改到别的脚上** ——
#:    而"该响的不响"在现场极难定位。**"不设防"与"能改硬件"不能同时成立。**
#:
#: 引脚仍然可以**手工改文件**（改完 `validate.py` 会查撞脚）。
#: ⚠️ 顺带说明：撞脚检查**并没有**因此变成死代码 —— :meth:`ConfigStore.validate`
#: 每次写盘前都对**整份合并配置**跑 `find_config_conflicts`，
#: 所以"手工把两个器件改到同一个脚、再来面板保存"照样会被拦（有测试钉住）。
DEVICE_PATCH_FIELDS = ("enabled", "read_interval_s", "optional")

#: 顶层**允许**修改的字段（其余一律报错，防止把 `mqtt`/`onenet` 从面板里误删）。
PATCH_TOP_FIELDS = ("thresholds", "devices")

#: 设备字段的**生效默认值**：文件里没写这个键时，等于这个值。
#: 用途（2026-10-01）：判断"用户其实没改"时必须拿**生效值**比，而不是拿"文件里有没有写"比 ——
#: 否则把面板原样提交回来会给每个设备补上 `enabled: true` / `read_interval_s: 1.0`，
#: 结果是"什么都没动却改了一次配置文件"（churn，而且每次都多一个备份文件）。
DEVICE_EFFECTIVE_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "optional": False,
    "read_interval_s": 1.0,
}


@dataclass
class PatchResult:
    """一次"部分合并"的结果（**还没落盘**）。"""

    raw: Dict[str, Any]
    config: AppConfig
    changed: List[str] = field(default_factory=list)


class ConfigStore:
    """一个配置文件的安全读写器。

    Args:
        path: 基础配置文件（通常是 ``rpi/config/devices.json``）。
        use_local: 校验时是否把 ``devices.local.json`` 合并进来（默认 True，与真实运行一致）。
            ⚠️ **测试应当传 False**：否则断言会随"这台机器上有没有本机覆盖文件"而变
            （2026-09-22 真机踩过同一类坑，见 `load_config` 的 docstring）。
        clock: 时间源（备份文件名的时间戳用它；测试可注入固定值）。
    """

    def __init__(self, path: str | Path, *, use_local: bool = True,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.use_local = bool(use_local)
        self._clock = clock

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def read_raw(self) -> Dict[str, Any]:
        """读**基础配置文件**的原始 JSON（深拷贝，调用方可以随便改）。

        基础文件不存在时退回示例配置（与 :func:`load_config` 的回退一致），
        这样"第一次跑起来就打开面板"也能看到并保存配置 —— 保存会把基础文件**建出来**。
        """
        if self.path.exists():
            return copy.deepcopy(_load_json_object(self.path))
        example = example_config_path()
        if example.exists() and example.resolve() != self.path.resolve():
            _LOG.info("基础配置不存在，配置面板以示例配置为底稿：%s", example)
            return copy.deepcopy(_load_json_object(example))
        raise ConfigError(f"配置文件不存在：{self.path}")

    def snapshot(self) -> Dict[str, Any]:
        """给面板看的配置视图：**全部阈值** + 每个设备的开关/周期/可选标志。

        刻意**不返回 `params`**：面板不做引脚编辑（用户要求），
        返回了反而容易被前端顺手渲染成可编辑项。
        """
        raw = self.read_raw()
        defaults = Thresholds()
        th_node = raw.get("thresholds") or {}
        thresholds = {
            name: th_node.get(name, getattr(defaults, name))
            for name in Thresholds.__dataclass_fields__
        }
        devices: Dict[str, Any] = {}
        for name, item in (raw.get("devices") or {}).items():
            if not isinstance(item, Mapping):
                continue
            devices[name] = {
                "driver": str(item.get("driver", "")),
                "enabled": bool(item.get("enabled", True)),
                "read_interval_s": item.get("read_interval_s", 1.0),
                "optional": bool(item.get("optional", False)),
            }

        warnings: List[str] = []
        local = read_local_overrides() if self.use_local else {}
        if local:
            merged = merge_override(raw, local)
            merged_th = merged.get("thresholds") or {}
            for name, value in thresholds.items():
                if name in merged_th and merged_th[name] != value:
                    warnings.append(
                        f"阈值 {name} 被 devices.local.json 覆盖成 {merged_th[name]}"
                        f"（面板显示/编辑的是基础文件里的 {value}）"
                    )
            merged_dev = merged.get("devices") or {}
            for name, info in devices.items():
                over = merged_dev.get(name) or {}
                for field_name in ("enabled", "read_interval_s"):
                    if field_name in over and over[field_name] != info[field_name]:
                        warnings.append(
                            f"设备 {name}.{field_name} 被 devices.local.json 覆盖成 "
                            f"{over[field_name]}（面板显示/编辑的是基础文件里的 {info[field_name]}）"
                        )
        return {
            "config_path": str(self.path),
            "use_local": self.use_local,
            "thresholds": thresholds,
            "devices": devices,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # 合并 + 校验（不落盘）
    # ------------------------------------------------------------------

    def apply_patch(self, patch: Mapping[str, Any]) -> PatchResult:
        """把 ``patch`` 部分合并到基础配置上，并**校验**（失败抛 ``ConfigError``）。

        调用方拿到结果后再决定要不要 :meth:`save` —— 这样"校验失败"与"写盘"天然分开，
        "校验失败 ⇒ 文件一个字节都没动"是结构上保证的，不靠调用顺序自觉。
        """
        if not isinstance(patch, Mapping):
            raise ConfigError("请求体必须是一个 JSON 对象")
        unknown = set(patch) - set(PATCH_TOP_FIELDS)
        if unknown:
            raise ConfigError(
                f"不认识的顶层字段 {sorted(unknown)}；只支持 {list(PATCH_TOP_FIELDS)}"
                "（mqtt / onenet 等其它配置不通过面板修改，也不允许被清掉）"
            )

        raw = self.read_raw()
        changed: List[str] = []
        self._patch_thresholds(raw, patch.get("thresholds") or {}, changed)
        self._patch_devices(raw, patch.get("devices") or {}, changed)
        config = self.validate(raw)
        return PatchResult(raw=raw, config=config, changed=changed)

    def validate(self, raw: Mapping[str, Any]) -> AppConfig:
        """校验一份（待写入的）基础配置。**这是写盘前的唯一判据。**

        校验对象是"基础 + 本机覆盖"合并后的结果 —— 因为那才是真正会跑起来的东西。
        """
        merged: Mapping[str, Any] = merge_override(raw, read_local_overrides()) if self.use_local else raw
        config = AppConfig.from_dict(merged)          # 未知键 / 阈值 / 周期都在这里报错
        conflicts = find_config_conflicts(merged)     # ★ 撞脚检查（刻意不过滤 enabled）
        if conflicts:
            raise ConfigError("存在 GPIO 撞脚：\n" + "\n".join(conflicts))
        return config

    # ------------------------------------------------------------------
    # 写（备份 + 原子替换）
    # ------------------------------------------------------------------

    def save(self, raw: Mapping[str, Any]) -> Optional[str]:
        """备份旧文件后**原子**写回基础配置文件。

        Returns:
            备份文件路径；基础文件原先不存在（首次创建）时返回 ``None``。
        """
        target = self.path
        if example_config_path().exists() and target.resolve() == example_config_path().resolve():
            raise ConfigError("拒绝写入 devices.example.json（那是模板，不是运行配置）")
        target.parent.mkdir(parents=True, exist_ok=True)

        backup: Optional[Path] = None
        if target.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._clock()))
            backup = target.with_name(f"{target.name}.bak-{stamp}")
            if backup.exists():
                # 同一秒内连按两次保存：**不能覆盖上一份备份**，否则回滚点就没了
                backup = target.with_name(f"{target.name}.bak-{stamp}-{os.getpid()}")
            shutil.copy2(target, backup)

        text = json.dumps(dict(raw), ensure_ascii=False, indent=2) + "\n"
        tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())     # 先落盘，再替换 —— 掉电也不会留半个 JSON
            os.replace(tmp, target)           # 同一目录内替换：POSIX/Windows 都是原子的
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        _LOG.info("配置文件已更新：%s（备份：%s）", target, backup or "无（首次创建）")
        return str(backup) if backup else None

    # ------------------------------------------------------------------
    # 内部：部分合并
    # ------------------------------------------------------------------

    def _patch_thresholds(self, raw: Dict[str, Any], patch: Mapping[str, Any],
                          changed: List[str]) -> None:
        if not isinstance(patch, Mapping):
            raise ConfigError("thresholds 必须是一个对象")
        known = set(Thresholds.__dataclass_fields__)
        bad = set(patch) - known
        if bad:
            raise ConfigError(
                f"阈值里有不认识的字段 {sorted(bad)}；可用字段：{sorted(known)}"
                "（拼错键不会报错、只会静默失效，所以这里刻意严格）"
            )
        node = raw.get("thresholds")
        if node is None:
            node = {}
            raw["thresholds"] = node
        if not isinstance(node, dict):
            raise ConfigError("配置里的 thresholds 不是一个对象（文件可能被手工改坏了）")
        defaults = Thresholds()
        for name, value in patch.items():
            _require_number(f"thresholds.{name}", value)
            # ★ 与**生效值**比较并**只在真的不一样时写**：文件里没写的阈值等于 dataclass 默认值，
            #   若无条件 `node[name] = value`，面板原样提交就会把二十来个默认阈值灌进用户的文件。
            old = node.get(name, getattr(defaults, name))
            if old == value:
                continue
            changed.append(f"thresholds.{name}: {old} → {value}")
            node[name] = value

    def _patch_devices(self, raw: Dict[str, Any], patch: Mapping[str, Any],
                       changed: List[str]) -> None:
        if not isinstance(patch, Mapping):
            raise ConfigError("devices 必须是一个对象")
        node = raw.get("devices")
        if not isinstance(node, dict):
            raise ConfigError("配置里的 devices 不是一个对象（文件可能被手工改坏了）")
        for name, item in patch.items():
            if not isinstance(item, Mapping):
                raise ConfigError(f"设备 {name} 的改动必须是一个对象")
            if name not in node:
                # 刻意不允许"改出一个新设备"：新增器件要连驱动参数一起想清楚，
                # 不是面板上点一下的事；拼错设备名也不该静默新建一个空壳。
                raise ConfigError(f"配置里没有设备 {name}（面板不允许新增设备，也不接受拼错的设备名）")
            target = node[name]
            if not isinstance(target, dict):
                raise ConfigError(f"配置里的设备 {name} 不是一个对象")
            bad = set(item) - set(DEVICE_PATCH_FIELDS)
            if bad:
                raise ConfigError(
                    f"设备 {name} 里有不支持的字段 {sorted(bad)}；可用字段：{list(DEVICE_PATCH_FIELDS)}"
                )
            for field_name, value in item.items():
                if field_name in ("enabled", "optional"):
                    if not isinstance(value, bool):
                        raise ConfigError(f"{name}.{field_name} 必须是布尔值（true / false）")
                else:                     # read_interval_s
                    _require_number(f"{name}.read_interval_s", value)
                # ★ 与**生效值**比较（文件里没写 = 默认值），而不是与"文件里有没有写"比较：
                #   这样"面板原样提交"是一个真正的 no-op，不会给每个设备补上默认键。
                old = target.get(field_name, DEVICE_EFFECTIVE_DEFAULTS.get(field_name, _MISSING))
                if old == value:
                    continue
                changed.append(f"devices.{name}.{field_name}: {old} → {value}")
                target[field_name] = value

    def _patch_params_unused_note(self) -> None:  # pragma: no cover - 占位说明，不参与运行
        """（占位）`params` 的递归合并**已按决定移除**，见 :data:`DEVICE_PATCH_FIELDS` 的说明。

        留这一行是因为它是个**容易被重新引入**的地方：将来若有人想"顺便支持改引脚"，
        必须先回答两个问题 —— ① 用户是否扩大了范围；② 接口是否仍然不设防。
        两个都答"是"才能加回来，并且必须同时保留撞脚校验。
        """


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _load_json_object(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")   # 本项目一律 UTF-8，绝不依赖系统默认编码
    except OSError as exc:
        raise ConfigError(f"读取配置失败：{path}：{exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 JSON 语法错误：{path} 第 {exc.lineno} 行：{exc.msg}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是对象：{path}")
    return data


#: "这个键原本不存在"的哨兵。用它而不是 ``None``：``None`` 可能是配置里**真的**写着的值，
#: 两者混在一起会让 `changed` 文案变成"None → None"这种看不出所以然的句子。
_MISSING = object()


def _require_number(label: str, value: Any) -> None:
    """数值字段必须是数字。

    ``bool`` 在 Python 里是 ``int`` 的子类，``True`` 会冒充 ``1`` —— 显式排除掉，
    否则 ``{"hr_min": true}`` 会被静默当成 1（这类"合法的错值"最难查）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} 必须是数字，收到 {value!r}（{type(value).__name__}）")


__all__ = ["ConfigStore", "PatchResult", "DEVICE_PATCH_FIELDS", "PATCH_TOP_FIELDS"]
