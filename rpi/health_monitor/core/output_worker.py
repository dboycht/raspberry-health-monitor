"""输出下发的工作线程：**让可弃的输出（声音）绝不阻塞不可停的监护循环**。

为什么需要它（`ERROR.md` **E63**，2026-10-01 真机实测）
------------------------------------------------------
真机上出现"两个按键同一秒报 `sensor_fault` 又同一秒自恢复"，查到底是：

1. 启动/报警时要播一句语音，`bt_speaker.speak()` **串行跑两条外部命令**
   （`espeak-ng` 合成 + `aplay` 播放），**每条各 15 秒超时** ⇒ 一次播报最坏 **30 秒**；
2. 而 `AlarmDispatcher.dispatch()` 是在**主循环线程里同步执行**的 ⇒
   这 30 秒里**不采集、不判报警、不响按键、不上云**（原始日志里 OneNET 上报整整空了 25 秒）；
3. 主循环一停，`Collector.snapshot()` 看到"所有周期短的设备都陈旧了" ⇒
   凭空报一串 `sensor_fault`（`detail.error` 明写"数据陈旧"）。

本模块把**音频类**指令（蜂鸣器 / 音箱）搬到一个工作线程里执行：

* 主循环只做"入队"，**立即返回**；
* 灯与屏仍在主线程同步下发（它们是一次 GPIO/I2C/SPI 写，微秒级），
  这样"报警最要紧的头几十毫秒"里灯与屏**仍是即时**的，不必等线程调度；
* 真机上的退化场景（本项目的音频是永久坏的：Pi 5 无 3.5 mm 孔、T8 已取消）
  靠 :attr:`OutputWorker.backoff_s` **熔断**：某器件连续失败后进入冷却期，
  冷却期内**不再入队**（也就不再白白占着工作线程等 15 秒超时）。

纪律（与 `docs` 里的安全红线一致）
----------------------------------
* 工作线程里**任何异常都不许抛穿**：一个坏音箱不能让监护停摆；
* 队列有上限（:data:`DEFAULT_QUEUE_SIZE`）：报警密集时**丢新保旧**并计数，
  绝不无限占用内存；
* :meth:`close` 幂等，且**排空后**才退出（最多等 ``drain_timeout_s``）。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Dict, Optional, Set

_LOG = logging.getLogger(__name__)

#: 队列上限。报警密集 + 输出器件卡死时，宁可丢弃后面的提示，也不让内存无限涨。
DEFAULT_QUEUE_SIZE = 128
#: 某器件连续失败达到该次数后进入冷却（熔断）。
DEFAULT_FAIL_BACKOFF_AFTER = 3
#: 冷却时长（秒）。冷却期内不再往该器件下发，避免"每帧都等一次超时"。
DEFAULT_BACKOFF_S = 60.0


class OutputWorker:
    """把输出指令搬到后台线程执行的执行器。

    Args:
        drain_timeout_s: :meth:`close` 最多等多久把队列跑完（秒）。
            小一点更"快停"，但会丢掉正在播的那一句；大一点更"说完再停"。
            默认 1.0 —— 停止服务时不该等 30 秒。
        backoff_s: 连续失败 :data:`DEFAULT_FAIL_BACKOFF_AFTER` 次后的冷却时长（秒）；
            ``<= 0`` 表示关闭熔断（仍会每次尝试）。
        queue_size: 队列上限。

    线程安全：:meth:`submit` 可从任意线程调用（主循环、HTTP 线程）；
    :attr:`sent` / :attr:`failures` / :attr:`dropped` 由工作线程写、主线程读，
    在 CPython 下都是**单条字典/整数操作**，读到的是"稍旧的合法值"而不是坏值
    （本项目刻意不为诊断计数加锁：它们只用于日志与状态展示）。
    """

    def __init__(
        self,
        drain_timeout_s: float = 1.0,
        backoff_s: float = DEFAULT_BACKOFF_S,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.drain_timeout_s = float(drain_timeout_s)
        self.backoff_s = float(backoff_s)
        self.clock = clock
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=max(1, int(queue_size)))
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        #: 曾经验证"根本没打开成功"的器件名（只用于日志措辞，不参与逻辑）
        self._opened_ok: Set[str] = set()
        #: ``{器件名: 冷却截止时刻}``：熔断用
        self._cooldown_until: Dict[str, float] = {}
        self.sent: Dict[str, int] = {}
        self.failures: Dict[str, int] = {}
        self.dropped: int = 0
        self.errors: list = []

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """启动工作线程（幂等）。"""
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="output-worker", daemon=True)
        self._thread.start()

    def close(self, drain: bool = True) -> None:
        """停止工作线程（幂等）。``drain=True`` 表示等队列跑完（最多 ``drain_timeout_s``）。"""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        if drain:
            deadline = time.monotonic() + self.drain_timeout_s
            while thread.is_alive() and time.monotonic() < deadline:
                thread.join(timeout=0.05)
        else:
            thread.join(timeout=0.2)

    # ------------------------------------------------------------------
    # 入队
    # ------------------------------------------------------------------

    def ready_at(self, name: str) -> float:
        """该器件"可以再试"的时刻（冷却中则返回未来的时刻）。"""
        return self._cooldown_until.get(name, 0.0)

    def in_backoff(self, name: str) -> bool:
        return self.clock() < self._cooldown_until.get(name, 0.0)

    def submit(self, name: str, device: Any, command: Any) -> bool:
        """把一条指令排给工作线程，**立即返回**。

        Returns:
            ``True`` = 已入队；``False`` = 被丢弃（队列满，或该器件正在冷却）。
        """
        if self.in_backoff(name):
            self.dropped += 1
            return False
        try:
            self._queue.put_nowait((name, device, command))
        except queue.Full:
            # ⚠️ 刻意"丢新保旧"：队列里的旧指令更接近"报警发生的那一刻"。
            self.dropped += 1
            return False
        return True

    def flush(self, timeout: float = 5.0) -> bool:
        """等队列跑空（**给测试与"关服务前排空"用**），返回是否真的空。

        实现是"插一个哨兵并等它被处理"：哨兵入队后，排在它前面的都已完成。
        哨兵本身不会被下发。
        """
        if not self.running:
            return self._queue.empty()
        done = threading.Event()
        try:
            self._queue.put((None, done, None))
        except Exception:  # pragma: no cover - 队列理论上不会在这里抛
            return False
        got = done.wait(timeout=max(0.0, timeout))
        return got and self._queue.empty()

    # ------------------------------------------------------------------
    # 工作线程
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            name, device, command = item
            if name is None:                      # flush 哨兵
                done = device
                try:
                    done.set()
                except Exception:  # pragma: no cover
                    pass
                self._queue.task_done()
                continue
            try:
                self._run_one(name, device, command)
            except Exception as exc:  # noqa: BLE001 - 工作线程绝不能死
                _LOG.debug("输出工作线程异常（已忽略）：%s: %s", type(exc).__name__, exc)
            finally:
                self._queue.task_done()

    def _run_one(self, name: str, device: Any, command: Any) -> None:
        """真正下发一条指令（在工作线程里跑）。"""
        try:
            device.send(command)
        except Exception as exc:  # noqa: BLE001 - 一个坏输出不许影响别的输出
            count = self.failures.get(name, 0) + 1
            self.failures[name] = count
            if count == 1:
                msg = f"{name}（{type(device).__name__}）后台下发失败：{type(exc).__name__}: {exc}"
                self.errors.append(msg)
                _LOG.warning("输出下发失败（不影响监护循环，也不影响其它输出）：%s", msg)
            elif count == DEFAULT_FAIL_BACKOFF_AFTER and self.backoff_s > 0:
                # ★ 熔断：连续失败到这个次数就进冷却，冷却期内不再往它下发。
                #   本项目真实场景：音频永久坏（Pi 5 无 3.5 mm 孔）⇒
                #   以前是**每次报警都白白等 15 秒**，现在只在前 3 次尝试。
                self._cooldown_until[name] = self.clock() + self.backoff_s
                msg = (f"{name} 连续 {count} 次下发失败（{type(exc).__name__}: {exc}），"
                       f"已熔断 {self.backoff_s:.0f} 秒不再尝试；监护循环不受影响")
                self.errors.append(msg)
                _LOG.warning("%s", msg)
            else:
                _LOG.debug("输出 %s 第 %d 次后台下发失败：%s: %s", name, count, type(exc).__name__, exc)
            return
        self.sent[name] = self.sent.get(name, 0) + 1
        if self.failures.pop(name, None) is not None:
            # 恢复了（比如音箱插回来）：立刻撤掉冷却，恢复正常下发
            self._cooldown_until.pop(name, None)
            _LOG.info("输出 %s 已恢复，撤销熔断", name)

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        now = self.clock()
        return {
            "running": self.running,
            "pending": self._queue.qsize(),
            "sent": dict(self.sent),
            "failures": dict(self.failures),
            "dropped": self.dropped,
            "backoff_until": {k: round(v - now, 1) for k, v in self._cooldown_until.items() if v > now},
            "errors": [str(x) for x in self.errors[-3:]],
        }


__all__ = ["OutputWorker", "DEFAULT_QUEUE_SIZE", "DEFAULT_FAIL_BACKOFF_AFTER", "DEFAULT_BACKOFF_S"]
