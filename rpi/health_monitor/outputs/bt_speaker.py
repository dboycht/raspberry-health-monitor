"""蓝牙音箱语音播报驱动（本地 TTS 合成 + 蓝牙出声）。

模块用途
--------
把报警"说出来"，这是本项目对老人最友好的一条通道（不用看屏幕、不用掏手机）：

- ``ALL_CLEAR`` → "心率已恢复正常，请注意休息"
- ``SOS_PRESSED`` → "已收到紧急求助，正在通知家属"
- ``NO_MOTION_TOO_LONG`` → "检测到长时间没有活动，请确认是否安全"

技术方案（**离线可用，不依赖网络**）
------------------------------------
``espeak-ng`` 合成中文 → 写成临时 WAV → ``aplay``/``paplay`` 送到蓝牙音箱：

1. ``espeak-ng -v zh -s 150 -w /tmp/xxx.wav "文本"``（合成，离线，秒级）
2. ``aplay -D <sink> /tmp/xxx.wav``（播放到 PulseAudio / bluealsa 的 sink）

⚠️ 为什么不用联网 TTS（百度/讯飞）：课程设计的演示现场往往没有稳定网络，
报警播报"卡住"比"声音难听"严重得多。espeak-ng 的音质确实机械，但**永远可用**。

接线表（本驱动无 GPIO；"接线"= 系统层配置）
--------------------------------------------
===================  ==============================  ==================================
项目                  位置                            说明
===================  ==============================  ==================================
蓝牙音箱              系统音频输出                    先用 ``bluetoothctl`` 配对连接
TTS 引擎              ``/usr/bin/espeak-ng``          ``sudo apt install -y espeak-ng``
播放器                ``/usr/bin/aplay``              ``sudo apt install -y alsa-utils``
===================  ==============================  ==================================

真机系统层配置（按顺序执行，**这些必须在树莓派上手动做一次**）
--------------------------------------------------------------
.. code-block:: bash

    # 1) 装工具
    sudo apt update && sudo apt install -y espeak-ng alsa-utils pulseaudio-utils bluez bluealsa
    # 2) 进蓝牙交互界面配对音箱（音箱要先进入配对模式）
    bluetoothctl
      > power on
      > agent on
      > default-agent
      > scan on          # 找到音箱 MAC，如 AA:BB:CC:DD:EE:FF
      > pair AA:BB:CC:DD:EE:FF
      > trust AA:BB:CC:DD:EE:FF     # trust 之后开机自动连
      > connect AA:BB:CC:DD:EE:FF
      > scan off
      > quit
    # 3) 让系统默认输出到蓝牙音箱（二选一）
    #    A. PulseAudio（推荐，桌面版自带，兼容性最好）
    pactl list short sinks                 # 看有哪些 sink
    pactl set-default-sink bluez_sink.AA_BB_CC_DD_EE_FF.a2dp_sink
    #    B. bluealsa（无桌面/最小系统，把 bluealsa 当 ALSA 设备用）
    aplay -D bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp /usr/share/sounds/alsa/Front_Center.wav
    # 4) 验证（不听声音也能查：看命令是否报错）
    espeak-ng -v zh -s 150 -w /tmp/t.wav "测试" && aplay /tmp/t.wav

**bluealsa vs PulseAudio 怎么选**：装了桌面（含 pipewire/pulseaudio）就用 PulseAudio，
``sink`` 留空走默认输出即可；最小系统（Lite）没有 PulseAudio，就装 ``bluealsa``
并显式传 ``sink="bluealsa:DEV=<MAC>,PROFILE=a2dp"``。

防吵人设计（安全设计，务必保留）
--------------------------------
1. **文本去重窗口**（``dedup_window_s``，默认 10 秒）：同一句话 10 秒内只播一次。
   报警引擎是按轮询周期反复下发的，不去重就会"心率高、心率高、心率高…"念个不停，
   既盖住了别的语音，又会让老人烦躁到拔电源。
   —— 注意去重是**允许重播**的：窗口一过，同样的文本仍会播（报警还在，就必须继续提醒）。
2. **历史条数上限**（``history_size``，默认 20）：``spoken_history`` 只留最近 N 条，
   长时间运行不会把内存吃光（也避免日志文件被语音文本灌爆）。

设计要点
--------
1. **外部命令封装成可替换的执行器**：所有 ``subprocess`` 调用都走构造参数
   ``runner``（默认 :func:`subprocess.run`）。单测注入**假执行器**，就能断言
   "到底执行了哪两条命令、参数对不对"，而**绝不会真的发出声音**。
2. **mock 模式绝不执行外部命令**：``mock=True`` 时只记录 ``spoken_history``，
   连 ``shutil.which`` 都不查（防止 mock 演示时因为缺 espeak-ng 而报错）。
3. **临时文件一定清理**：合成出来的 WAV 用完就删，否则 ``/tmp`` 会被慢慢塞满
   （树莓派 SD 卡写满会导致整个系统起不来）。

负责人：________（待分配）
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from ..hal.device import OutputDevice
from ..hal.exceptions import (
    AlarmDispatchError,
    DeviceInitError,
    UnsupportedError,
)
from ..hal.models import DeviceKind, Sample, Severity, SpeakCommand

_log = logging.getLogger(__name__)

#: 默认去重窗口（秒）：同一句话在窗口内只播一次（防吵人，见模块文档）。
DEFAULT_DEDUP_WINDOW_S = 10.0
#: 默认历史长度：只留最近 N 条播报文本。
DEFAULT_HISTORY_SIZE = 20
#: self_check 里要检查的外部程序（缺哪个就提示装哪个）
REQUIRED_PROGRAMS = ("espeak-ng", "aplay")


class BtSpeaker(OutputDevice):
    """蓝牙音箱语音播报（espeak-ng 合成 → aplay 播放）。

    Args:
        device_name: 蓝牙设备名/备注（仅用于日志与文档；真正决定声音去哪的是 ``sink``）。
        sink: 播放设备/接收器名称。留空 = 系统默认输出（PulseAudio 下最省事）。
              最小系统用 bluealsa 时形如
              ``"bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp"``。
        engine: TTS 可执行文件名，默认 ``"espeak-ng"``。
        rate: 语速（词/分），默认 150。老人听，建议 120~150，别超过 180。
        voice: 音色/语言，默认 ``"zh"``（中文）。可传 ``"zh+f3"`` 之类。
        player: 播放器，默认 ``"aplay"``；PulseAudio 环境也可用 ``"paplay"``。
        dedup_window_s: 文本去重窗口（秒），默认 10.0；<=0 表示不去重。
        history_size: ``spoken_history`` 保留条数，默认 20。
        timeout_s: 单条外部命令超时（秒），默认 15.0（防止 aplay 卡死拖住主流程）。
        which: **可注入**的"查可执行文件"函数，默认 :func:`shutil.which`。
        runner: **可注入**的外部命令执行器，默认 :func:`subprocess.run`。
                签名 ``runner(args, **kwargs)``，返回值无所谓（只用于排障）。
                单测注入假执行器即可断言命令且不发声。
        clock: **可注入时钟**（默认 :func:`time.monotonic`），去重窗口用。
        tmp_dir: 临时 WAV 目录，默认系统临时目录。
        mock: 模拟模式。**True 时绝不执行任何外部命令**。
        name: 实例名（默认 ``bt_speaker``）。
    """

    KIND = DeviceKind.AUDIO
    NAME = "bt_speaker"

    def __init__(
        self,
        device_name: str = "",
        sink: str = "",
        engine: str = "espeak-ng",
        rate: int = 150,
        voice: str = "zh",
        player: str = "aplay",
        dedup_window_s: float = DEFAULT_DEDUP_WINDOW_S,
        history_size: int = DEFAULT_HISTORY_SIZE,
        timeout_s: float = 15.0,
        which: Callable[[str], Optional[str]] = shutil.which,
        runner: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
        tmp_dir: Optional[str] = None,
        bus: Any = None,
        mock: bool = False,
        name: str = "",
    ) -> None:
        # bus 本驱动用不到（声音走系统音频栈而不是 I2C/SPI），保留是为了统一构造签名：
        # 注册表 create_device() 在 mock=True 时会注入一个 MockBus。
        super().__init__(bus=bus, mock=mock, name=name)
        self.device_name = str(device_name)
        self.sink = str(sink)
        self.engine = str(engine)
        self.rate = int(rate)
        self.voice = str(voice)
        self.player = str(player)
        self.dedup_window_s = float(dedup_window_s)
        self.history_size = max(1, int(history_size))
        self.timeout_s = max(0.1, float(timeout_s))
        self.which: Callable[[str], Optional[str]] = which
        self.runner: Callable[..., Any] = runner
        self.clock: Callable[[], float] = clock
        self.tmp_dir = tmp_dir

        self._history: Deque[str] = deque(maxlen=self.history_size)
        self._last_spoken: Dict[str, float] = {}   # 规范化文本 → 上次播报时刻
        self._skipped_dup = 0                      # 因去重被跳过的次数
        self._spoken_count = 0                     # 实际播报次数
        self._commands_run = 0                     # 实际执行的外部命令条数
        self._wav_seq = 0                          # 临时文件名序号（保证同进程内不重名）

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def open(self) -> None:
        """检查运行前提（mock 模式什么都不做）。

        Raises:
            DeviceInitError: 真实模式下缺少 ``espeak-ng`` / ``aplay``。
        """
        if self._opened:
            return
        if self.mock:
            self._opened = True
            return
        check = self.self_check()
        if not check["ok"]:
            raise DeviceInitError(
                f"蓝牙音箱语音播报初始化失败：{check['detail']}。"
                "其余排查：1) 音箱是否已用 bluetoothctl 配对并 connect；"
                "2) 系统默认输出是否指向蓝牙（`pactl list short sinks` 后 "
                "`pactl set-default-sink <名称>`）；"
                "3) 若用 bluealsa，请显式传 sink='bluealsa:DEV=<MAC>,PROFILE=a2dp'；"
                "4) PC 上开发请用 mock=True"
            )
        self._opened = True

    def close(self) -> None:
        """释放资源（本驱动无常驻资源；幂等、不抛异常）。"""
        self._opened = False

    # ------------------------------------------------------------------
    # 外部命令拼装
    # ------------------------------------------------------------------

    def synth_command(self, text: str, wav_path: str) -> List[str]:
        """拼出合成命令：``espeak-ng -v zh -s 150 -w <wav> <文本>``。"""
        return [
            self.engine,
            "-v", self.voice,
            "-s", str(self.rate),
            "-w", wav_path,
            str(text),
        ]

    def play_command(self, wav_path: str) -> List[str]:
        """拼出播放命令：``aplay [-D <sink>] <wav>``。"""
        cmd = [self.player]
        if self.sink:
            cmd += ["-D", self.sink]
        cmd.append(wav_path)
        return cmd

    def _run(self, cmd: List[str]) -> None:
        """执行一条外部命令（走可注入的 ``runner``）。"""
        self._commands_run += 1
        _log.debug("执行外部命令：%s", " ".join(shlex.quote(c) for c in cmd))
        self.runner(cmd, check=True, timeout=self.timeout_s)

    def _temp_wav_path(self) -> str:
        """生成一个本次播报专用的临时 WAV 路径。

        ⚠️ 用**真实时钟**的纳秒值 + **进程内自增计数器**：
        - 可注入时钟可能是被测的假时钟（会长时间停在同一个值），不能用来命名；
        - Windows 上 ``time_ns()`` 的两次连续调用也可能拿到同一个值，
          所以再叠一个自增计数，保证同一进程内绝不重名（两次播报不会互相覆盖）。
        """
        directory = self.tmp_dir or tempfile.gettempdir()
        self._wav_seq += 1
        stamp = f"{os.getpid():d}_{time.time_ns():d}_{self._wav_seq:d}"
        return os.path.join(directory, f"hm_speak_{stamp}.wav")

    # ------------------------------------------------------------------
    # 指令入口
    # ------------------------------------------------------------------

    def send(self, command: Any) -> None:
        """执行一条语音播报指令。

        Args:
            command: :class:`~health_monitor.hal.models.SpeakCommand`。

        Raises:
            DeviceNotReady: 未 ``open()``。
            UnsupportedError: 指令类型不对（**绝不静默忽略**）。
            AlarmDispatchError: 合成或播放失败。
        """
        self._require_open()
        if not isinstance(command, SpeakCommand):
            exc = UnsupportedError(
                f"蓝牙音箱不支持指令类型 {type(command).__name__}；"
                "它只接受 SpeakCommand(text='...', priority=Severity.X)"
            )
            self._note_fault(exc)
            raise exc

        text = str(command.text).strip()
        if not text:
            # 空文本不是"错误"（业务层可能拼出了空串），但**绝不发声**，也不计一次播报
            _log.warning("蓝牙音箱收到空文本的 SpeakCommand，已忽略（不发声）")
            self._note_ok()
            return

        try:
            spoken = self.speak(text, priority=command.priority)
        except Exception as exc:  # noqa: BLE001 - 统一翻译成报警下发失败
            self._note_fault(exc)
            if isinstance(exc, AlarmDispatchError):
                raise
            raise AlarmDispatchError(
                f"蓝牙音箱播报失败（文本 {text!r}）：{exc}。"
                "排查：`espeak-ng -v zh -s 150 -w /tmp/t.wav '测试'` 与 `aplay /tmp/t.wav` "
                "能否单独跑通；蓝牙音箱是否已连接（`bluetoothctl info <MAC>`）"
            ) from exc
        self._note_ok()
        _log.info(
            "语音播报%s：%s（优先级 %s）",
            "" if spoken else "（去重跳过）",
            text,
            command.priority.name if isinstance(command.priority, Severity) else command.priority,
        )

    # ------------------------------------------------------------------
    # 核心：去重 + 播报
    # ------------------------------------------------------------------

    def speak(self, text: str, priority: Severity = Severity.NOTICE) -> bool:
        """播报一句话。

        Returns:
            ``True`` = 真的播了；``False`` = 被去重窗口挡下（没播）。

        Note:
            ``priority`` 目前只用于日志与"紧急语速更快"这一层，
            **不改变去重策略**（去重对新文本永远放行，紧急播报不会被饿死）。
        """
        self._require_open()
        if not self._should_speak(text):
            self._skipped_dup += 1
            _log.info("语音播报去重：%.0f 秒内已播过 %r，本次跳过", self.dedup_window_s, text)
            return False

        if self.mock:
            self._record(text)
            return True

        cmd_synth, cmd_play, wav_path = self._build_commands(text, priority)
        try:
            self._run(cmd_synth)
            self._run(cmd_play)
        finally:
            # 临时 WAV 一定要删：树莓派 SD 卡写满会直接起不来
            try:
                if os.path.exists(wav_path):
                    os.remove(wav_path)
            except OSError as exc:  # noqa: BLE001 - 清理失败不该影响播报结果
                _log.warning("临时语音文件 %s 删除失败：%s", wav_path, exc)
        self._record(text)
        return True

    def _build_commands(
        self, text: str, priority: Severity
    ) -> tuple[List[str], List[str], str]:
        """拼出（合成命令, 播放命令, 临时 wav 路径）。"""
        wav_path = self._temp_wav_path()
        rate = self._rate_for(priority)
        saved_rate, self.rate = self.rate, rate
        try:
            synth = self.synth_command(text, wav_path)
        finally:
            self.rate = saved_rate
        return synth, self.play_command(wav_path), wav_path

    def _rate_for(self, priority: Severity) -> int:
        """紧急播报语速略快（15% 上限），但不超过 180，避免听不清。"""
        try:
            if priority is not None and int(priority) >= int(Severity.CRITICAL):
                return min(180, int(round(self.rate * 1.15)))
        except (TypeError, ValueError):  # pragma: no cover - 传了非 Severity 的值
            pass
        return self.rate

    # ------------------------------------------------------------------
    # 去重
    # ------------------------------------------------------------------

    def _normalise(self, text: str) -> str:
        """规范化文本：去掉首尾空白 + 折叠内部连续空白。

        这样 ``"心率 偏高"`` 与 ``"心率  偏高 "`` 会被认作同一句话。
        """
        return " ".join(str(text).split())

    def _should_speak(self, text: str) -> bool:
        """判断这句话现在能不能播（去重窗口，见模块文档"防吵人设计"）。"""
        key = self._normalise(text)
        if self.dedup_window_s <= 0:
            return True
        now = self.clock()
        last = self._last_spoken.get(key)
        if last is None or (now - last) >= self.dedup_window_s:
            self._last_spoken[key] = now
            return True
        return False

    def _record(self, text: str) -> None:
        self._history.append(text)
        self._spoken_count += 1

    # ------------------------------------------------------------------
    # 状态 / 自检 / 文档
    # ------------------------------------------------------------------

    @property
    def spoken_history(self) -> List[str]:
        """最近播报过的文本（最多 ``history_size`` 条，最新的在最后）。"""
        return list(self._history)

    @property
    def last_spoken(self) -> Optional[str]:
        """最近一次播报的文本；从未播报过时为 ``None``。"""
        return self._history[-1] if self._history else None

    def read(self) -> Sample:
        """回读自身状态（基类 ``Sample``；细节走 :meth:`status`）。"""
        self._require_open()
        self._note_ok()
        return Sample(device=self.name)

    def self_check(self) -> Dict[str, Any]:
        """自检：``espeak-ng`` 与 ``aplay`` 是否都在（**这是最有价值的一次自检**）。

        缺哪个就给出**可直接复制的安装命令**——演示前一晚跑一次，
        就不会出现"现场报警了却没声音"的尴尬。

        Returns:
            ``{"ok": bool, "detail": str, "programs": {名字: 路径或 None}}``
        """
        if self.mock:
            return {
                "ok": True,
                "detail": "mock 模式：不检查外部程序，也不会真的发声",
                "programs": {},
            }
        programs: Dict[str, Optional[str]] = {}
        missing: List[str] = []
        for prog in (self.engine, self.player):
            try:
                path = self.which(prog)
            except Exception as exc:  # noqa: BLE001 - which 本身失败也要如实上报
                path = None
                _log.warning("检查外部程序 %s 时出错：%s", prog, exc)
            programs[prog] = path
            if not path:
                missing.append(prog)
        if missing:
            hints = "；".join(self._install_hint(p) for p in missing)
            return {
                "ok": False,
                "detail": f"缺少 {'、'.join(missing)}：{hints}",
                "programs": programs,
            }
        return {
            "ok": True,
            "detail": "espeak-ng 与 aplay 均就绪；"
            f"播放目标={self.sink or '系统默认输出'}"
            f"{'（设备名 ' + self.device_name + '）' if self.device_name else ''}",
            "programs": programs,
        }

    @staticmethod
    def _install_hint(program: str) -> str:
        """给缺失的外部程序一条可直接复制的安装命令。"""
        mapping = {
            "espeak-ng": "sudo apt install -y espeak-ng",
            "aplay": "sudo apt install -y alsa-utils",
            "paplay": "sudo apt install -y pulseaudio-utils",
        }
        return mapping.get(program, f"sudo apt install -y {program}")

    def status(self) -> Dict[str, Any]:
        """扩展基类状态：暴露播报次数、去重跳过次数与去重窗口。"""
        info = super().status()
        info.update(
            {
                "device_name": self.device_name,
                "sink": self.sink or "系统默认输出",
                "engine": self.engine,
                "voice": self.voice,
                "rate": self.rate,
                "player": self.player,
                "spoken_count": self._spoken_count,
                "skipped_dup": self._skipped_dup,
                "commands_run": self._commands_run,
                "dedup_window_s": self.dedup_window_s,
                "history": self.spoken_history,
            }
        )
        return info

    def describe(self) -> Dict[str, Any]:
        """接线/配置说明（会被 ``docs`` 生成脚本读取）。"""
        info = super().describe()
        info.update(
            {
                "bus": "系统音频（PulseAudio / bluealsa）；无 GPIO，不占 I2C",
                "pins": {
                    "vcc": "音箱自带电源（蓝牙音箱不需要树莓派供电）",
                    "audio": f"蓝牙 A2DP → {self.sink or '系统默认 sink'}",
                },
                "commands": {
                    "synth": " ".join(self.synth_command("<文本>", "<临时.wav>")),
                    "play": " ".join(self.play_command("<临时.wav>")),
                },
                "notes": (
                    "离线 TTS（espeak-ng），不依赖网络；"
                    f"同一句话 {self.dedup_window_s:.0f} 秒内只播一次（防吵人）；"
                    f"历史保留最近 {self.history_size} 条；"
                    "配对/默认输出设置见本文件模块文档与 docs（bluetoothctl / pactl / bluealsa）"
                ),
            }
        )
        return info


__all__ = [
    "BtSpeaker",
    "DEFAULT_DEDUP_WINDOW_S",
    "DEFAULT_HISTORY_SIZE",
    "REQUIRED_PROGRAMS",
]
