"""运行进度：案例进程报告已发生的事，只有主进程向终端写入，两个案例不会抢同一行。"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
from queue import Empty
import shutil
import sys
import threading
import time

from progress_display import render_batch, render_event, wrap_terminal

BEIJING = timezone(timedelta(hours=8))
_case = None  # 一个工作进程只处理一个案例；同案例的 L1 线程共享这个出口。
_lock = threading.Lock()


def _stamp():
    return datetime.now(BEIJING).strftime("%H:%M:%S")


@contextmanager
def case_output(handle, channel=None, *, redactor=str):
    """与案例 run.log 的重定向一起使用；直接调用工作流时不强制打印。"""
    global _case
    previous, _case = _case, (handle, channel, redactor)
    try:
        yield
    finally:
        _case = previous


def _clean(value, redact):
    if isinstance(value, str):
        return " ".join(redact(value).split())
    if isinstance(value, (list, tuple)):
        return [_clean(x, redact) for x in value]
    if isinstance(value, dict):
        return {k: _clean(v, redact) for k, v in value.items()}
    return value


def emit(kind, **fields):
    """同一条摘要写入案例日志，再发给批次。这里不接收完整模型正文或原始遥测。"""
    if _case is None:
        return
    handle, channel, redact = _case
    with _lock:
        event = {**_clean(fields, redact), "kind": kind, "case_handle": handle, "time": _stamp()}
        print(render_event(event, color=False, icons=False), flush=True)
        if channel is not None:
            channel.put(event)


def _terminal_style(stream):
    """只在可交互且支持的终端用颜色/图标。PowerShell 重定向和文件日志都用纯文本。"""
    if not stream.isatty():
        return False, False
    try:
        "🔎↩".encode(stream.encoding or "ascii")
        icons = True
    except UnicodeEncodeError:
        icons = False
    color = "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    if color and os.name == "nt":
        import ctypes
        import msvcrt

        handle = ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno()))
        mode = ctypes.c_ulong()
        console = ctypes.windll.kernel32
        color = bool(
            console.GetConsoleMode(handle, ctypes.byref(mode))
            and console.SetConsoleMode(handle, mode.value | 0x0004)
        )
    return color, icons


class BatchProgress:
    """主进程是终端和批次 run.log 的唯一写入方；等待中的调用按案例、角色分别计时。"""

    def __init__(self, log, total, interval):
        self.log, self.total, self.interval = log, total, interval
        self.stream = sys.stdout
        self.interactive = self.stream.isatty()
        self.color, self.icons = _terminal_style(self.stream)
        self.started = time.monotonic()
        self.last = {}  # 案例 -> (上次进展时间，事件)
        self.pending = {}  # (案例, 角色, model/tool) -> (开始时间，事件)
        self.heartbeat_at = {}  # 单独计时，不能让快案例的持续输出掩盖另一例的长等待。
        self.chain_totals = {}
        self.width = shutil.get_terminal_size((120, 25)).columns

    def _write(self, plain, styled):
        self.log.write(plain + "\n")
        self.log.flush()
        if self.stream is None:
            return
        try:
            try:
                self.stream.write(styled + "\n")
            except UnicodeEncodeError:
                # 旧终端代码页不一定能显示每个符号；UTF-8 文件保留原文。
                encoding = self.stream.encoding or "utf-8"
                self.stream.write(styled.encode(encoding, "replace").decode(encoding) + "\n")
            self.stream.flush()
        except OSError:
            self.stream = None  # 终端管道关闭，已经启动的调查仍继续写日志。

    def show(self, kind, **fields):
        self.receive({"kind": kind, "time": _stamp(), **fields})

    def receive(self, event):
        event = {**event, "width": self.width}
        kind, handle = event["kind"], event.get("case_handle")
        if handle and kind != "heartbeat":
            now = time.monotonic()
            self.last[handle] = (now, event)
            if kind == "chain_created":
                self.chain_totals[handle] = self.chain_totals.get(handle, 0) + 1
            if kind in {"model_start", "tool_start", "model_end", "tool_end"}:
                task, direction = kind.split("_")
                key = (handle, event.get("agent_id"), task)
                if direction == "start":
                    self.pending[key] = (now, event)
                else:
                    self.pending.pop(key, None)
            if kind == "batch_case_end":
                self.last.pop(handle)
                self.heartbeat_at.pop(handle, None)
                self.chain_totals.pop(handle, None)
                for key in [key for key in self.pending if key[0] == handle]:
                    del self.pending[key]
        # 阶段分隔用留白，不清屏、不覆盖历史，滚回去仍能完整阅读。
        if kind in {"batch_end", "batch_case_start"} or (
            kind == "stage_start" and event.get("stage") in {"seed", "meta", "synthesis", "output"}
        ):
            self._write("", "")
        plain = render_event(event, color=False, icons=False)
        styled = (
            render_event(event, color=self.color, icons=self.icons) if self.color or self.icons else plain
        )
        self._write(plain, wrap_terminal(styled, self.width) if self.interactive else styled)

    def drain(self, channel):
        while True:
            try:
                event = channel.get_nowait()
            except Empty:
                return
            self.receive(event)

    def bar(self, done, running, queued):
        args = (done, self.total, running, queued, time.monotonic() - self.started)
        self._write(
            render_batch(*args, color=False, icons=False, width=self.width),
            render_batch(*args, color=self.color, icons=self.icons, width=self.width),
        )

    def heartbeat(self):
        now = time.monotonic()
        for handle, (seen, last) in self.last.items():
            if now - self.heartbeat_at.get(handle, self.started) < self.interval:
                continue
            waits = []
            for (owner, agent, task), (start, event) in self.pending.items():
                if owner == handle and now - start >= self.interval:
                    what = "等模型" if task == "model" else f"执行工具 {event.get('tool', '')}"
                    path = " → ".join(event.get("nodes", []))
                    subject = ""
                    if path:
                        no, total = event.get("case_chain_no"), self.chain_totals.get(handle)
                        which = f"本例第 {no} 条，已建 {total} 条；" if no and total else ""
                        subject = f"{event['chain_id']}（{which}{path}）"
                    stage = {
                        "seed": "Seed 候选调查",
                        "l1": "L1 走链",
                        "l2": "L2 审核",
                        "meta": "Meta 仲裁",
                        "synthesis": "答案综合",
                    }.get(event.get("stage"), agent)
                    waits.append(
                        f"{stage} {subject} {what} #{event.get('call_no', '?')}，已 {int(now - start)} 秒"
                    )
            if waits:
                self.show("heartbeat", stage="case", case_handle=handle, detail="；".join(waits))
            elif now - seen >= self.interval:
                # 例如：子进程还在启动或写结果；沉默并不足以判断发生故障。
                self.show(
                    "heartbeat",
                    stage="case",
                    case_handle=handle,
                    detail=(
                        f"距上一条进展 {int(now - seen)} 秒；上条记录 "
                        + render_event(last, color=False, icons=False)
                    ),
                )
            else:
                continue
            self.heartbeat_at[handle] = now
