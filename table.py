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


PALETTE = ["\033[36m", "\033[33m", "\033[35m", "\033[32m", "\033[34m", "\033[31m"]
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
USE_COLOR = sys.stdout.isatty()
_print_lock = threading.Lock()


def say(name: str, color: str, header: str, body: str) -> None:
    with _print_lock:
        if USE_COLOR:
            print(f"\n{color}{BOLD}◆ {name}{RESET}{DIM} · {header}{RESET}\n{body}\n")
        else:
            print(f"\n◆ {name} · {header}\n{body}\n")


def say_system(msg: str) -> None:
    with _print_lock:
        if USE_COLOR:
            print(f"\n{DIM}── {msg} ──{RESET}")
        else:
            print(f"\n── {msg} ──")


def default_intervention() -> str | None:
    """非阻塞检查 stdin：讨论中按过 Enter → 在批次边界弹出输入提示。非 TTY 恒为 None。"""
    if not sys.stdin.isatty():
        return None
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if not ready:
        return None
    sys.stdin.readline()  # 消费触发的回车
    with _print_lock:
        try:
            line = input("你说（空回车继续 · /stop 提前收尾）: ").strip()
        except EOFError:
            return None
    return line or None


def preflight(pcs: list[ParticipantConfig], store: RunStore) -> None:
    """开会前逐个 ping CLI，验证认证与可用性；失败中止。"""
    say_system("启动预检")
    for pc in pcs:
        probe = dataclasses.replace(pc, timeout=60)
        try:
            out = call_cli(probe, "这是连通性测试。请只回复：PONG",
                           store.sandbox, store.audit, "preflight")
        except CliError as exc:
            raise SystemExit(f"预检失败 — {exc}\n（确认该 CLI 已登录；或用 --skip-preflight 跳过）")
        note = "" if "PONG" in out.upper() else "（未见 PONG，但回复非空，放行）"
        say_system(f"预检 {pc.name}: ok {note}")


def run(topic: str, pcs: list[ParticipantConfig], max_rounds: int, max_context_chars: int,
        runs_dir: pathlib.Path, intervention=None, do_preflight: bool = True) -> int:
    disc = tp.Discussion(topic, [p.name for p in pcs], max_rounds)
    store = RunStore(runs_dir, topic)
    by_name = {p.name: p for p in pcs}
    colors = {p.name: PALETTE[i % len(PALETTE)] for i, p in enumerate(pcs)}
    check_input = intervention if intervention is not None else default_intervention
    say_system(f"运行目录：{store.dir}")

    if do_preflight:
        preflight(pcs, store)

    stopped = False

    def checkpoint() -> bool:
        """批次边界检查人类插话。返回 True 表示 /stop。"""
        nonlocal stopped
        text = check_input()
        if text is None:
            return False
        if text.strip() == "/stop":
            stopped = True
            disc.add_note("Human", "[/stop] 人工提前终止，进入终局输出")
            store.transcript(disc)
            store.state(disc)
            return True
        label = disc.add_constraint(text.strip())
        say("Human", "", f"插话 {label} · 已作废当前版本全部票", text.strip())
        store.transcript(disc)
        store.state(disc)
        return False

    def speak(pc: ParticipantConfig, prompt: str, parser, purpose: str):
        try:
            parsed, raw = call_and_parse(pc, prompt, parser, store.sandbox, store.audit, purpose)
        except CliError as exc:
            say(pc.name, colors[pc.name], purpose, f"[调用失败] {exc}")
            return "error", str(exc)
        if parsed is None:
            store.raw(pc.name, raw)
            say(pc.name, colors[pc.name], purpose, "[两次输出均无法解析，本次缺票，原文已存档 raw/]")
            return "invalid", raw
        return "ok", parsed

    def batch(jobs):
        """jobs: list[(pc, prompt, parser, purpose)] → {name: (status, payload)}。并行即盲审。"""
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futs = {ex.submit(speak, *job): job[0].name for job in jobs}
            return {futs[f]: f.result() for f in concurrent.futures.as_completed(futs)}

    def editor_call(build, purpose: str) -> bool:
        """按轮转尝试主编调用；失败顺延下一位；全员失败返回 False。"""
        order = disc.participants
        idx = order.index(disc.next_editor())
        for step in range(len(order)):
            name = order[(idx + step) % len(order)]
            pc = by_name[name]
            status, payload = speak(pc, build(pc), tp.parse_editor, purpose)
            if status == "ok":
                speech, text, changelog = payload
                draft = disc.add_draft(name, text, changelog, speech)
                say(name, colors[name], f"{purpose} → {draft.version_id}", speech or "（无说明）")
                return True
            disc.add_note(name, f"主编调用失败（{purpose}），轮转下一位")
        return False

    # ── 阶段0：独立立论（并行、互不可见） ──
    say_system("阶段0：独立立论")
    jobs = [(pc, tp.build_proposal_prompt(topic, disc.constraints, pc.name, pc.lens),
             tp.parse_proposal, "proposal") for pc in pcs]
    for name, (status, payload) in batch(jobs).items():
        if status == "ok":
            analysis, prop = payload
            disc.add_proposal(name, analysis, prop)
            say(name, colors[name], "独立立论", f"{analysis}\n\n【独立方案】\n{prop}")
        else:
            disc.add_note(name, "阶段0未产出独立方案")
    store.transcript(disc)
    store.state(disc)
    if not disc.proposals:
        say_system("全部参与者阶段0失败，中止")
        return 1

    # ── 阶段1：合成 v1 ──
    if not checkpoint():
        say_system("阶段1：合成 v1")
        if not editor_call(
            lambda pc: tp.build_merge_prompt(topic, disc.constraints, pc.name, pc.lens,
                                             disc.proposals),
            "merge",
        ):
            say_system("全部主编候选失败，中止")
            return 1
        store.transcript(disc)
        store.state(disc)

    # ── 评审周期 ──
    if not stopped and disc.current is not None:
        for cycle in range(1, max_rounds + 1):
            disc.cycle = cycle
            targets = disc.pending_reviewers()
            if targets:
                say_system(f"周期 {cycle}/{max_rounds}：评审 {disc.current.version_id}"
                           f"（{', '.join(targets)}）")
                ctx = tp.render_transcript(disc.events, max_context_chars)
                jobs = [(by_name[n],
                         tp.build_review_prompt(topic, disc.constraints, n, by_name[n].lens,
                                                disc.current, ctx, cycle == 1),
                         tp.parse_verdict, f"review-c{cycle}") for n in targets]
                res = batch(jobs)
                for n in targets:
                    status, payload = res[n]
                    if status == "ok":
                        speech, pv = payload
                        disc.record_vote(n, pv, speech)
                        say(n, colors[n],
                            f"评审 {disc.current.version_id} → {pv.verdict.value}", speech)
                    else:
                        disc.record_invalid(
                            n, "输出无法解析" if status == "invalid" else f"调用失败：{payload}")
                store.transcript(disc)
                store.state(disc)
            if checkpoint():
                break
            if disc.all_reviewers_accepted() and not disc.consensus_reached():
                author = disc.current.author
                say_system(f"全体评审通过，作者 {author} 最后确认（生成≠审查）")
                ctx = tp.render_transcript(disc.events, max_context_chars)
                status, payload = speak(
                    by_name[author],
                    tp.build_confirm_prompt(topic, disc.constraints, author,
                                            by_name[author].lens, disc.current, ctx),
                    tp.parse_verdict, f"confirm-c{cycle}")
                if status == "ok":
                    speech, pv = payload
                    disc.record_vote(author, pv, speech)
                    say(author, colors[author], f"作者确认 → {pv.verdict.value}", speech)
                else:
                    disc.record_invalid(
                        author, "确认票无法解析" if status == "invalid" else f"调用失败：{payload}")
                store.transcript(disc)
                store.state(disc)
                if checkpoint():
                    break
            if disc.consensus_reached():
                break
            if cycle == max_rounds:
                break
            if disc.needs_revision():
                say_system(f"周期 {cycle}：主编修订")
                ctx = tp.render_transcript(disc.events, max_context_chars)
                if not editor_call(
                    lambda pc: tp.build_revision_prompt(topic, disc.constraints, pc.name,
                                                        pc.lens, disc.current,
                                                        disc.active_blockers(), ctx),
                    f"revise-c{cycle}",
                ):
                    say_system("全部主编候选失败，提前终局")
                    break
                store.transcript(disc)
                store.state(disc)
                if checkpoint():
                    break

    # ── 终局 ──
    result = disc.outcome()
    recommendation = None
    if result is not tp.Result.CONSENSUS and disc.proposals:
        name = disc.next_editor()
        say_system(f"未达成共识，征询 {name} 的个人建议（明确标注，不构成共识）")
        ctx = tp.render_transcript(disc.events, max_context_chars)
        status, payload = speak(
            by_name[name],
            tp.build_recommendation_prompt(topic, disc.constraints, name, by_name[name].lens,
                                           disc.current, disc.active_blockers(), ctx),
            lambda t: t.strip() or None, "recommendation")
        if status == "ok":
            recommendation = payload
            say(name, colors[name], "主编个人建议", recommendation)
    store.final(tp.render_final(disc, recommendation))
    store.state(disc)
    say_system(f"结果：{result.value} → {store.dir / 'final.md'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="table", description="多 AI 圆桌辩论")
    ap.add_argument("topic", help="讨论议题（纯文本）")
    ap.add_argument("--max-rounds", type=int, default=5, help="评审周期上限（默认 5）")
    ap.add_argument("--config", type=pathlib.Path, default=None,
                    help="参与者配置 TOML（默认自动找 ./table.toml）")
    ap.add_argument("--max-context-chars", type=int, default=120_000,
                    help="讨论记录拼装上限字符数（默认 120000）")
    ap.add_argument("--runs-dir", type=pathlib.Path, default=pathlib.Path("runs"))
    ap.add_argument("--skip-preflight", action="store_true", help="跳过启动预检")
    args = ap.parse_args(argv)
    if args.max_rounds < 1:
        raise SystemExit("--max-rounds 至少为 1")
    pcs = load_config(args.config)
    try:
        return run(args.topic, pcs, args.max_rounds, args.max_context_chars,
                   args.runs_dir, do_preflight=not args.skip_preflight)
    except KeyboardInterrupt:
        print("\n已中断；transcript/state/session 均已即时落盘。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
