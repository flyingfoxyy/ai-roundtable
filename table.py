#!/usr/bin/env python3
"""Table —— 多 AI 圆桌辩论工具（IO 壳）。协议纯逻辑见 table_protocol.py。"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import dataclasses
import datetime
import json
import os
import pathlib
import select
import signal
import subprocess
import sys
import threading
import time
import tomllib

import table_protocol as tp


@dataclasses.dataclass(frozen=True)
class ParticipantConfig:
    name: str
    cmd: tuple[str, ...]
    lens: str = ""
    timeout: int = 300


DEFAULT_PARTICIPANTS = [
    ParticipantConfig("Claude", ("claude", "-p"), "重点审查：可维护性与长期复杂度"),
    ParticipantConfig("Codex", ("codex", "exec", "--skip-git-repo-check", "-"),
                      "重点审查：工程落地成本与迁移风险"),
    ParticipantConfig("Gemini", ("gemini",), "重点审查：性能、扩展性与运维负担"),
]


def load_config(path: pathlib.Path | None) -> list[ParticipantConfig]:
    """path=None：若 ./table.toml 存在则用之，否则内置默认。显式 path 不存在 → SystemExit。"""
    if path is None:
        auto = pathlib.Path("table.toml")
        if not auto.exists():
            return list(DEFAULT_PARTICIPANTS)
        path = auto
    elif not path.exists():
        raise SystemExit(f"配置文件不存在：{path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    pcs: list[ParticipantConfig] = []
    for item in data.get("participants", []):
        if not item.get("name") or not item.get("cmd"):
            raise SystemExit("每个 participant 需要 name 和非空 cmd")
        pcs.append(ParticipantConfig(item["name"], tuple(item["cmd"]),
                                     item.get("lens", ""), int(item.get("timeout", 300))))
    if len(pcs) < 2:
        raise SystemExit("至少需要 2 名参与者")
    if len({p.name for p in pcs}) != len(pcs):
        raise SystemExit("参与者名字必须唯一")
    return pcs


class CliError(Exception):
    pass


def call_cli(pc: ParticipantConfig, prompt: str, cwd, audit, purpose: str) -> str:
    """调用参与者 CLI（stdin→stdout）。超时/失败自动重试一次；超时击杀整个进程组。"""
    last = "未知错误"
    for attempt in (1, 2):
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
               "participant": pc.name, "purpose": purpose, "attempt": attempt,
               "cmd": list(pc.cmd), "prompt_chars": len(prompt)}
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(pc.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=cwd, text=True,
                                    start_new_session=True)
        except FileNotFoundError:
            rec["outcome"] = "not-found"
            audit(rec)
            raise CliError(f"{pc.name}: 命令不存在 {pc.cmd[0]}")
        try:
            out, err = proc.communicate(prompt, timeout=pc.timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            rec.update(outcome="timeout", secs=round(time.monotonic() - t0, 1))
            audit(rec)
            last = f"{pc.name}: 超时 {pc.timeout}s"
            continue
        rec.update(outcome="ok" if proc.returncode == 0 else "error",
                   exit=proc.returncode, secs=round(time.monotonic() - t0, 1),
                   resp_chars=len(out), stderr_tail=err.strip()[-500:])
        audit(rec)
        if proc.returncode == 0 and out.strip():
            return out
        last = f"{pc.name}: 退出码 {proc.returncode}"
    raise CliError(last)


def call_and_parse(pc: ParticipantConfig, prompt: str, parser, cwd, audit, purpose: str):
    """调用 + 严格解析；失败发回恰好一次格式纠错；仍失败返回 (None, 最后原文)。"""
    raw = call_cli(pc, prompt, cwd, audit, purpose)
    parsed = parser(raw)
    if parsed is not None:
        return parsed, raw
    fix = (f"{prompt}\n\n【格式纠错】你上一次的输出缺少要求的标记行，无法解析。"
           f"请重新输出**完整**回复并严格遵守上述输出格式。你上一次的输出是：\n{raw[:4000]}")
    raw = call_cli(pc, fix, cwd, audit, f"{purpose}/format-retry")
    parsed = parser(raw)
    return (parsed, raw) if parsed is not None else (None, raw)


class RunStore:
    """运行目录与事件级即时落盘。"""

    def __init__(self, base: pathlib.Path, topic: str):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
        slug = tp.sanitize_slug(topic)
        d = base / f"{stamp}-{slug}"
        n = 2
        while d.exists():
            d = base / f"{stamp}-{slug}-{n}"
            n += 1
        self.dir = d
        self.sandbox = d / "sandbox"
        self.sandbox.mkdir(parents=True)
        (d / "raw").mkdir()
        (d / "transcript.md").write_text(f"# 圆桌讨论\n\n**议题：** {topic}\n", encoding="utf-8")
        self._written_events = 0

    def transcript(self, disc: tp.Discussion) -> None:
        new = disc.events[self._written_events:]
        if not new:
            return
        with (self.dir / "transcript.md").open("a", encoding="utf-8") as f:
            f.write(tp.render_events_md(new))
            f.flush()
            os.fsync(f.fileno())
        self._written_events = len(disc.events)

    def state(self, disc: tp.Discussion) -> None:
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(tp.snapshot(disc), ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.dir / "state.json")

    def audit(self, rec: dict) -> None:
        with (self.dir / "session.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def raw(self, name: str, text: str) -> None:
        n = len(list((self.dir / "raw").iterdir())) + 1
        (self.dir / "raw" / f"{n:03d}-{name}.txt").write_text(text, encoding="utf-8")

    def final(self, text: str) -> None:
        (self.dir / "final.md").write_text(text, encoding="utf-8")
