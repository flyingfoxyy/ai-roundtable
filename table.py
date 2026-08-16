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
