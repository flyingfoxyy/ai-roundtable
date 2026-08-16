# Table 多 AI 圆桌辩论工具 · 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `table.py`：让 claude/codex/gemini 三个 CLI 按"独立立论 → 轮值主编合成 → 并行盲审 → 主编修订"协议辩论一个纯文本议题，产出严格版本绑定的共识方案或如实标注分歧的终局报告。

**Architecture:** 两模块分层——`table_protocol.py` 是无 IO 纯逻辑（状态机、解析、prompt 拼装、渲染），`table.py` 是 IO 壳（子进程适配、落盘、终端交互、主循环）。测试三层：纯逻辑单元测试、假 CLI 集成测试、真实冒烟。

**Tech Stack:** Python ≥3.11（本机 3.12.3），仅标准库（tomllib/subprocess/concurrent.futures/select/unittest）。无 pytest——测试全部用 `unittest`。

**Spec:** `docs/superpowers/specs/2026-08-15-table-roundtable-design.md`（本计划从该 spec 推导；执行者两份都要读）

## Global Constraints

- Python ≥ 3.11；运行与测试均零第三方依赖（测试跑 `python3 -m unittest`，不用 pytest）
- `table_protocol.py` 禁止任何 IO（无 open/subprocess/print）；`table.py` 持有全部 IO
- 所有 CLI 调用：prompt 走 stdin，回复取 stdout，cwd 固定为运行目录下的 `sandbox/`
- 子进程一律 `start_new_session=True`，超时用 `os.killpg` 杀整个进程组
- 输出标记行独占一行、严格匹配：`---PROPOSAL---` `---VERDICT---` `---DRAFT---` `---CHANGELOG---`
- 解析失败 → 恰好一次格式纠错重试 → 仍失败记 INVALID（原文存档 `raw/`），绝不猜测语义
- 共识不变量：全体参与者对同一 `v{n}-{sha256前8位}` 版本号有有效 ACCEPT；新版本/人类插话作废全部旧票；缺票绝不静默计为同意
- transcript.md / state.json / session.jsonl 每次事件后立即落盘（append+fsync 或 tmp+os.replace）
- 提交信息风格沿用仓库现状（`feat:` / `test:` / `docs:` + 中文描述）

## 文件结构

```
table.py               # IO 壳：配置、适配器、落盘、终端、主循环、入口（任务 6-10）
table_protocol.py      # 纯逻辑：解析、状态机、prompt、渲染（任务 1-5）
tests/
  __init__.py
  fake_cli.py          # 剧本驱动的假 CLI（任务 7）
  test_protocol.py     # 纯逻辑单元测试（任务 1-5）
  test_table.py        # 配置/适配器/落盘测试（任务 6-8）
  test_integration.py  # 全流程集成测试（任务 9-10）
.gitignore             # runs/ __pycache__/
README.md              # 任务 11
```

---

### Task 1: 脚手架与输出解析器

**Files:**
- Create: `table_protocol.py`, `tests/__init__.py`, `tests/test_protocol.py`, `.gitignore`

**Interfaces:**
- Produces: 常量 `MARKER_PROPOSAL/MARKER_VERDICT/MARKER_DRAFT/MARKER_CHANGELOG`；`Verdict(Enum)` ACCEPT/BLOCK/INVALID；`Blocker(severity, text)`；`ParsedVerdict(verdict, statement, blockers)`；`parse_proposal(text) -> tuple[str, str] | None`；`parse_verdict(text) -> tuple[str, ParsedVerdict] | None`；`parse_editor(text) -> tuple[str, str, str] | None`

- [ ] **Step 1: 写失败测试**

`.gitignore` 内容：

```
runs/
__pycache__/
```

`tests/__init__.py` 为空文件。`tests/test_protocol.py`：

```python
import unittest

import table_protocol as tp


class TestParsers(unittest.TestCase):
    def test_parse_proposal_ok(self):
        out = tp.parse_proposal("我的分析。\n---PROPOSAL---\n方案正文\n第二行")
        self.assertEqual(out, ("我的分析。", "方案正文\n第二行"))

    def test_parse_proposal_marker_missing_or_empty(self):
        self.assertIsNone(tp.parse_proposal("没有标记"))
        self.assertIsNone(tp.parse_proposal("分析\n---PROPOSAL---\n"))

    def test_parse_proposal_marker_with_surrounding_whitespace(self):
        self.assertEqual(tp.parse_proposal("a\n  ---PROPOSAL---  \nb"), ("a", "b"))

    def test_parse_verdict_accept_with_statement(self):
        speech, pv = tp.parse_verdict("论述\n---VERDICT---\nACCEPT: 最强反例是X；可容忍因为Y")
        self.assertEqual(speech, "论述")
        self.assertIs(pv.verdict, tp.Verdict.ACCEPT)
        self.assertIn("最强反例是X", pv.statement)

    def test_parse_verdict_accept_multiline_statement(self):
        _, pv = tp.parse_verdict("s\n---VERDICT---\nACCEPT\n反例在下一行")
        self.assertEqual(pv.statement, "反例在下一行")

    def test_parse_verdict_accept_without_statement_fails(self):
        self.assertIsNone(tp.parse_verdict("s\n---VERDICT---\nACCEPT"))
        self.assertIsNone(tp.parse_verdict("s\n---VERDICT---\nACCEPT：  "))

    def test_parse_verdict_block_with_graded_items(self):
        _, pv = tp.parse_verdict(
            "s\n---VERDICT---\nBLOCK\n- [硬伤] 忽略了数据一致性\n* [偏好] 命名不清\n- [待验证] QPS假设"
        )
        self.assertIs(pv.verdict, tp.Verdict.BLOCK)
        self.assertEqual(
            [(b.severity, b.text) for b in pv.blockers],
            [("硬伤", "忽略了数据一致性"), ("偏好", "命名不清"), ("待验证", "QPS假设")],
        )

    def test_parse_verdict_block_without_items_fails(self):
        self.assertIsNone(tp.parse_verdict("s\n---VERDICT---\nBLOCK\n就是不行"))

    def test_parse_verdict_missing_marker_or_garbage(self):
        self.assertIsNone(tp.parse_verdict("没有标记 ACCEPT"))
        self.assertIsNone(tp.parse_verdict("s\n---VERDICT---\nMAYBE"))
        self.assertIsNone(tp.parse_verdict("s\n---VERDICT---\n"))

    def test_parse_editor_ok(self):
        out = tp.parse_editor("说明\n---DRAFT---\n草案全文\n---CHANGELOG---\n- 处理了X")
        self.assertEqual(out, ("说明", "草案全文", "- 处理了X"))

    def test_parse_editor_missing_parts_fail(self):
        self.assertIsNone(tp.parse_editor("只有说明"))
        self.assertIsNone(tp.parse_editor("s\n---DRAFT---\n草案没有清单"))
        self.assertIsNone(tp.parse_editor("s\n---DRAFT---\n\n---CHANGELOG---\n清单"))
        self.assertIsNone(tp.parse_editor("s\n---DRAFT---\n草\n---CHANGELOG---\n"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_protocol -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'table_protocol'`）

- [ ] **Step 3: 实现解析器**

`table_protocol.py`：

```python
"""Table 圆桌协议纯逻辑：解析、状态机、prompt 拼装、渲染。禁止任何 IO。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum

MARKER_PROPOSAL = "---PROPOSAL---"
MARKER_VERDICT = "---VERDICT---"
MARKER_DRAFT = "---DRAFT---"
MARKER_CHANGELOG = "---CHANGELOG---"


class Verdict(Enum):
    ACCEPT = "ACCEPT"
    BLOCK = "BLOCK"
    INVALID = "INVALID"


@dataclass(frozen=True)
class Blocker:
    severity: str  # 硬伤 | 偏好 | 待验证
    text: str


@dataclass(frozen=True)
class ParsedVerdict:
    verdict: Verdict  # 仅 ACCEPT / BLOCK（INVALID 由适配层判定）
    statement: str
    blockers: tuple[Blocker, ...] = ()


def _split_on_marker(text: str, marker: str) -> tuple[str, str] | None:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == marker:
            return "\n".join(lines[:i]).strip(), "\n".join(lines[i + 1:]).strip()
    return None


def parse_proposal(text: str) -> tuple[str, str] | None:
    """阶段0输出 → (分析, 方案)；格式不符返回 None。"""
    split = _split_on_marker(text, MARKER_PROPOSAL)
    if split is None:
        return None
    analysis, proposal = split
    if not proposal:
        return None
    return analysis, proposal


_BLOCKER_RE = re.compile(r"^\s*[-*]\s*\[(硬伤|偏好|待验证)\]\s*(.+)$")


def parse_verdict(text: str) -> tuple[str, ParsedVerdict] | None:
    """评审/确认输出 → (公开论述, ParsedVerdict)；格式不符返回 None。"""
    split = _split_on_marker(text, MARKER_VERDICT)
    if split is None:
        return None
    speech, tail = split
    if not tail:
        return None
    first, _, rest = tail.partition("\n")
    head = first.strip()
    if head.upper().startswith("ACCEPT"):
        statement = (head[len("ACCEPT"):].strip(" :：") + "\n" + rest.strip()).strip()
        if not statement:
            return None  # ACCEPT 必须附残余风险声明
        return speech, ParsedVerdict(Verdict.ACCEPT, statement)
    if head.upper().startswith("BLOCK"):
        blockers = tuple(
            Blocker(m.group(1), m.group(2).strip())
            for line in tail.splitlines()
            if (m := _BLOCKER_RE.match(line))
        )
        if not blockers:
            return None  # BLOCK 必须列出分级问题
        return speech, ParsedVerdict(Verdict.BLOCK, tail, blockers)
    return None


def parse_editor(text: str) -> tuple[str, str, str] | None:
    """主编输出 → (论述, 草案全文, 变更清单)；格式不符返回 None。"""
    split = _split_on_marker(text, MARKER_DRAFT)
    if split is None:
        return None
    speech, tail = split
    split2 = _split_on_marker(tail, MARKER_CHANGELOG)
    if split2 is None:
        return None
    draft, changelog = split2
    if not draft or not changelog:
        return None
    return speech, draft, changelog
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_protocol -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add table_protocol.py tests/ .gitignore
git commit -m "feat: 输出解析器（严格标记 + 分级 blocker）"
```

---

### Task 2: 数据模型与投票账本

**Files:**
- Modify: `table_protocol.py`（追加）
- Test: `tests/test_protocol.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `Verdict/Blocker/ParsedVerdict`
- Produces: `Draft(number, text, author, changelog)` 含属性 `version_id -> str`（形如 `v3-a1b2c3d4`）；`Vote(participant, version_id, verdict, statement, blockers)`；`Event(kind, cycle, participant, text)`；`Discussion(topic, participants: list[str], max_rounds)` 及方法 `add_proposal(participant, analysis, proposal)`、`add_draft(author, text, changelog, speech="") -> Draft`、`record_vote(participant, parsed, speech) -> Vote`、`record_invalid(participant, reason)`、`add_note(participant, text)`、属性 `current -> Draft | None`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_protocol.py`）

```python
class TestLedger(unittest.TestCase):
    def make(self):
        return tp.Discussion("题", ["A", "B", "C"], 5)

    def test_requires_two_participants(self):
        with self.assertRaises(ValueError):
            tp.Discussion("题", ["A"], 5)

    def test_version_id_binds_content(self):
        d1 = tp.Draft(1, "文本", "A", "log")
        d2 = tp.Draft(1, "文本", "A", "log")
        d3 = tp.Draft(1, "别的文本", "A", "log")
        self.assertEqual(d1.version_id, d2.version_id)
        self.assertNotEqual(d1.version_id, d3.version_id)
        self.assertTrue(d1.version_id.startswith("v1-"))
        self.assertEqual(len(d1.version_id), 3 + 8)

    def test_new_draft_clears_all_votes(self):
        disc = self.make()
        disc.add_draft("A", "v1文", "分歧点")
        disc.record_vote("B", tp.ParsedVerdict(tp.Verdict.ACCEPT, "risk"), "s")
        disc.record_vote("C", tp.ParsedVerdict(tp.Verdict.ACCEPT, "risk"), "s")
        self.assertEqual(len(disc.votes), 2)
        disc.add_draft("B", "v2文", "改了")
        self.assertEqual(disc.votes, {})            # 含作者在内全部作废
        self.assertEqual(len(disc.vote_log), 2)     # 历史保留

    def test_record_vote_binds_current_version(self):
        disc = self.make()
        d1 = disc.add_draft("A", "v1文", "log")
        vote = disc.record_vote("B", tp.ParsedVerdict(tp.Verdict.ACCEPT, "r"), "s")
        self.assertEqual(vote.version_id, d1.version_id)

    def test_record_invalid_keeps_raw_out_of_votes_semantics(self):
        disc = self.make()
        disc.add_draft("A", "v1文", "log")
        disc.record_invalid("B", "两次解析失败")
        self.assertIs(disc.votes["B"].verdict, tp.Verdict.INVALID)

    def test_events_accumulate(self):
        disc = self.make()
        disc.add_proposal("A", "分析", "方案")
        disc.add_draft("A", "v1", "log", "说明")
        disc.add_note("B", "阶段0失败")
        kinds = [e.kind for e in disc.events]
        self.assertEqual(kinds, ["proposal", "draft", "note"])
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_protocol -v`
Expected: FAIL（`AttributeError: module 'table_protocol' has no attribute 'Discussion'` 等）

- [ ] **Step 3: 实现**（追加到 `table_protocol.py`）

```python
@dataclass(frozen=True)
class Draft:
    number: int
    text: str
    author: str
    changelog: str  # v1: 分歧点清单；其后: 变更清单

    @property
    def version_id(self) -> str:
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:8]
        return f"v{self.number}-{digest}"


@dataclass(frozen=True)
class Vote:
    participant: str
    version_id: str
    verdict: Verdict
    statement: str
    blockers: tuple[Blocker, ...] = ()


@dataclass(frozen=True)
class Event:
    kind: str  # proposal | draft | review | human | note
    cycle: int  # 0 = 阶段0/合成
    participant: str
    text: str


class Discussion:
    """协议状态机。纯内存，无 IO。"""

    def __init__(self, topic: str, participants: list[str], max_rounds: int):
        if len(participants) < 2:
            raise ValueError("至少需要 2 名参与者")
        self.topic = topic
        self.participants = list(participants)
        self.max_rounds = max_rounds
        self.constraints: list[str] = []
        self.proposals: dict[str, str] = {}
        self.drafts: list[Draft] = []
        self.votes: dict[str, Vote] = {}   # 当前版本的票（含 INVALID 占位）
        self.vote_log: list[Vote] = []     # 全部历史票（含被作废的）
        self.events: list[Event] = []
        self.cycle = 0
        self.unaddressed_constraints = False

    @property
    def current(self) -> Draft | None:
        return self.drafts[-1] if self.drafts else None

    def add_proposal(self, participant: str, analysis: str, proposal: str) -> None:
        self.proposals[participant] = proposal
        self.events.append(
            Event("proposal", 0, participant, f"{analysis}\n\n【独立方案】\n{proposal}")
        )

    def add_draft(self, author: str, text: str, changelog: str, speech: str = "") -> Draft:
        draft = Draft(len(self.drafts) + 1, text, author, changelog)
        self.drafts.append(draft)
        self.votes.clear()  # 新版本作废全部旧票（含作者）
        self.unaddressed_constraints = False
        body = f"{speech}\n\n【草案 {draft.version_id}】\n{text}\n\n【变更清单】\n{changelog}".strip()
        self.events.append(Event("draft", self.cycle, author, body))
        return draft

    def record_vote(self, participant: str, parsed: ParsedVerdict, speech: str) -> Vote:
        assert self.current is not None
        vote = Vote(participant, self.current.version_id, parsed.verdict,
                    parsed.statement, parsed.blockers)
        self.votes[participant] = vote
        self.vote_log.append(vote)
        self.events.append(Event(
            "review", self.cycle, participant,
            f"{speech}\n\n【表态 · {self.current.version_id}】{parsed.verdict.value}: {parsed.statement}",
        ))
        return vote

    def record_invalid(self, participant: str, reason: str) -> None:
        assert self.current is not None
        vote = Vote(participant, self.current.version_id, Verdict.INVALID, reason)
        self.votes[participant] = vote
        self.vote_log.append(vote)
        self.events.append(Event("note", self.cycle, participant, f"[本周期缺票] {reason}"))

    def add_note(self, participant: str, text: str) -> None:
        self.events.append(Event("note", self.cycle, participant, text))
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_protocol -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add table_protocol.py tests/test_protocol.py
git commit -m "feat: 数据模型与版本绑定投票账本"
```

---

### Task 3: 流程判定与共识状态机

**Files:**
- Modify: `table_protocol.py`（`Verdict` 后追加 `Result`；`Discussion` 内追加方法；模块尾部追加 `snapshot`）
- Test: `tests/test_protocol.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `Discussion/Draft/Vote`
- Produces: `Result(Enum)` CONSENSUS/NO_CONSENSUS/INCOMPLETE；`Discussion` 方法 `valid_vote(p) -> Vote | None`、`reviewers_of_current() -> list[str]`、`pending_reviewers() -> list[str]`、`all_reviewers_accepted() -> bool`、`has_any_block() -> bool`、`consensus_reached() -> bool`、`needs_revision() -> bool`、`next_editor() -> str`、`add_constraint(text) -> str`（返回 `H{n}` 标签）、`active_blockers() -> list[tuple[str, Blocker]]`、`outcome() -> Result`；模块级 `snapshot(disc) -> dict`

- [ ] **Step 1: 写失败测试**（追加）

```python
class TestFlow(unittest.TestCase):
    def make(self):
        disc = tp.Discussion("题", ["A", "B", "C"], 5)
        disc.add_draft("A", "v1文", "分歧点")
        return disc

    ACCEPT = tp.ParsedVerdict(tp.Verdict.ACCEPT, "残余风险")
    BLOCK = tp.ParsedVerdict(
        tp.Verdict.BLOCK, "raw", (tp.Blocker("硬伤", "缺一致性"),)
    )

    def test_pending_reviewers_excludes_author_and_valid_votes(self):
        disc = self.make()
        self.assertEqual(disc.pending_reviewers(), ["B", "C"])
        disc.record_vote("B", self.ACCEPT, "s")
        self.assertEqual(disc.pending_reviewers(), ["C"])   # 补征只找缺票者
        disc.record_invalid("C", "解析失败")
        self.assertEqual(disc.pending_reviewers(), ["C"])   # INVALID 仍是缺票

    def test_consensus_requires_author_confirmation(self):
        disc = self.make()
        disc.record_vote("B", self.ACCEPT, "s")
        disc.record_vote("C", self.ACCEPT, "s")
        self.assertTrue(disc.all_reviewers_accepted())
        self.assertFalse(disc.consensus_reached())          # 作者尚未确认
        disc.record_vote("A", self.ACCEPT, "s")
        self.assertTrue(disc.consensus_reached())
        self.assertIs(disc.outcome(), tp.Result.CONSENSUS)

    def test_author_self_block_prevents_consensus(self):
        disc = self.make()
        disc.record_vote("B", self.ACCEPT, "s")
        disc.record_vote("C", self.ACCEPT, "s")
        disc.record_vote("A", self.BLOCK, "s")
        self.assertFalse(disc.consensus_reached())
        self.assertTrue(disc.has_any_block())
        self.assertTrue(disc.needs_revision())

    def test_add_constraint_voids_votes_and_flags_revision(self):
        disc = self.make()
        disc.record_vote("B", self.ACCEPT, "s")
        label = disc.add_constraint("必须用Python")
        self.assertEqual(label, "H1")
        self.assertEqual(disc.votes, {})
        self.assertTrue(disc.unaddressed_constraints)
        self.assertTrue(disc.needs_revision())
        self.assertEqual(disc.constraints, ["必须用Python"])

    def test_next_editor_rotation_wraps(self):
        disc = tp.Discussion("题", ["A", "B", "C"], 5)
        self.assertEqual(disc.next_editor(), "A")           # 无草案 → P0
        disc.add_draft("A", "x", "l")
        self.assertEqual(disc.next_editor(), "B")
        disc.add_draft("C", "y", "l")
        self.assertEqual(disc.next_editor(), "A")           # C 之后回卷

    def test_outcome_paths(self):
        disc = tp.Discussion("题", ["A", "B"], 5)
        self.assertIs(disc.outcome(), tp.Result.INCOMPLETE)  # 成稿前终止
        disc.add_draft("A", "x", "l")
        self.assertIs(disc.outcome(), tp.Result.INCOMPLETE)  # 评审者缺票
        disc.record_invalid("B", "失败")
        self.assertIs(disc.outcome(), tp.Result.INCOMPLETE)  # INVALID
        disc.record_vote("B", self.BLOCK, "s")
        self.assertIs(disc.outcome(), tp.Result.NO_CONSENSUS)  # 全员表态但有 BLOCK

    def test_active_blockers_lists_by_participant(self):
        disc = self.make()
        disc.record_vote("B", self.BLOCK, "s")
        self.assertEqual(disc.active_blockers(), [("B", tp.Blocker("硬伤", "缺一致性"))])

    def test_snapshot_roundtrips_json_types(self):
        import json
        disc = self.make()
        disc.record_vote("B", self.ACCEPT, "s")
        snap = json.loads(json.dumps(tp.snapshot(disc)))
        self.assertEqual(snap["outcome"], "INCOMPLETE")
        self.assertEqual(snap["drafts"][0]["author"], "A")
        self.assertEqual(snap["votes"]["B"]["verdict"], "ACCEPT")
        self.assertEqual(len(snap["vote_log"]), 1)
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_protocol -v`
Expected: FAIL（缺 `Result`、`pending_reviewers` 等）

- [ ] **Step 3: 实现**

在 `Verdict` 类定义之后插入：

```python
class Result(Enum):
    CONSENSUS = "CONSENSUS"
    NO_CONSENSUS = "NO_CONSENSUS"
    INCOMPLETE = "INCOMPLETE"
```

在 `Discussion` 类内追加方法：

```python
    def valid_vote(self, participant: str) -> Vote | None:
        cur = self.current
        if cur is None:
            return None
        vote = self.votes.get(participant)
        if vote and vote.version_id == cur.version_id and vote.verdict is not Verdict.INVALID:
            return vote
        return None

    def reviewers_of_current(self) -> list[str]:
        assert self.current is not None
        return [p for p in self.participants if p != self.current.author]

    def pending_reviewers(self) -> list[str]:
        """当前版本上尚无有效票的评审者（缺票补征天然涵盖）。"""
        return [p for p in self.reviewers_of_current() if self.valid_vote(p) is None]

    def all_reviewers_accepted(self) -> bool:
        return all(
            (v := self.valid_vote(p)) is not None and v.verdict is Verdict.ACCEPT
            for p in self.reviewers_of_current()
        )

    def has_any_block(self) -> bool:
        return any(
            (v := self.valid_vote(p)) is not None and v.verdict is Verdict.BLOCK
            for p in self.participants
        )

    def consensus_reached(self) -> bool:
        if self.current is None:
            return False
        return all(
            (v := self.valid_vote(p)) is not None and v.verdict is Verdict.ACCEPT
            for p in self.participants
        )

    def needs_revision(self) -> bool:
        return self.has_any_block() or self.unaddressed_constraints

    def next_editor(self) -> str:
        if self.current is None:
            return self.participants[0]
        idx = self.participants.index(self.current.author)
        return self.participants[(idx + 1) % len(self.participants)]

    def add_constraint(self, text: str) -> str:
        self.constraints.append(text)
        self.votes.clear()  # 插话作废当前版本全部票
        self.unaddressed_constraints = True
        label = f"H{len(self.constraints)}"
        self.events.append(Event("human", self.cycle, "Human", f"[{label} · 绑定约束] {text}"))
        return label

    def active_blockers(self) -> list[tuple[str, Blocker]]:
        out: list[tuple[str, Blocker]] = []
        for p in self.participants:
            v = self.valid_vote(p)
            if v is not None and v.verdict is Verdict.BLOCK:
                out.extend((p, b) for b in v.blockers)
        return out

    def outcome(self) -> Result:
        if self.current is None:
            return Result.INCOMPLETE
        if self.consensus_reached():
            return Result.CONSENSUS
        cur_id = self.current.version_id
        has_invalid = any(
            v.version_id == cur_id and v.verdict is Verdict.INVALID
            for v in self.votes.values()
        )
        missing = any(self.valid_vote(p) is None for p in self.reviewers_of_current())
        if has_invalid or missing:
            return Result.INCOMPLETE
        return Result.NO_CONSENSUS
```

模块尾部追加：

```python
def _vote_dict(v: Vote) -> dict:
    return {
        "participant": v.participant,
        "version_id": v.version_id,
        "verdict": v.verdict.value,
        "statement": v.statement,
        "blockers": [{"severity": b.severity, "text": b.text} for b in v.blockers],
    }


def snapshot(disc: Discussion) -> dict:
    """state.json 用的可 JSON 化快照。"""
    return {
        "topic": disc.topic,
        "participants": disc.participants,
        "constraints": disc.constraints,
        "cycle": disc.cycle,
        "max_rounds": disc.max_rounds,
        "outcome": disc.outcome().value,
        "drafts": [
            {"number": d.number, "version_id": d.version_id, "author": d.author,
             "text": d.text, "changelog": d.changelog}
            for d in disc.drafts
        ],
        "votes": {p: _vote_dict(v) for p, v in disc.votes.items()},
        "vote_log": [_vote_dict(v) for v in disc.vote_log],
    }
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_protocol -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add table_protocol.py tests/test_protocol.py
git commit -m "feat: 共识状态机（版本绑定/清票/补征/终局判定）与快照"
```

---

### Task 4: prompt 构造、讨论记录渲染与截断、slug

**Files:**
- Modify: `table_protocol.py`（追加）
- Test: `tests/test_protocol.py`（追加）

**Interfaces:**
- Consumes: Task 1-3 的 `Draft/Blocker/Event`、各 MARKER 常量
- Produces: `sanitize_slug(topic, max_len=40) -> str`；`build_proposal_prompt(topic, constraints, name, lens) -> str`；`build_merge_prompt(topic, constraints, name, lens, proposals: dict[str, str]) -> str`；`build_review_prompt(topic, constraints, name, lens, draft, transcript, first_cycle) -> str`；`build_confirm_prompt(topic, constraints, name, lens, draft, transcript) -> str`；`build_revision_prompt(topic, constraints, name, lens, draft, blockers: list[tuple[str, Blocker]], transcript) -> str`；`build_recommendation_prompt(topic, constraints, name, lens, draft: Draft | None, blockers, transcript) -> str`；`render_transcript(events, max_chars=120_000) -> str`；`render_events_md(events) -> str`

- [ ] **Step 1: 写失败测试**（追加）

```python
class TestPrompts(unittest.TestCase):
    def test_sanitize_slug(self):
        self.assertEqual(tp.sanitize_slug("微服务 还是/单体？"), "微服务-还是-单体")
        self.assertEqual(tp.sanitize_slug("../../etc"), "etc")
        self.assertEqual(len(tp.sanitize_slug("长" * 100)), 40)
        self.assertEqual(tp.sanitize_slug("？！。"), "untitled")

    def test_proposal_prompt_contains_essentials(self):
        p = tp.build_proposal_prompt("选型题", ["必须用Python"], "Claude", "可维护性")
        for needle in ("Claude", "可维护性", "选型题", "H1. 必须用Python",
                       tp.MARKER_PROPOSAL, "看不到其他参与者"):
            self.assertIn(needle, p)

    def test_review_prompt_first_cycle_extra_rule(self):
        d = tp.Draft(1, "草案文", "A", "分歧点清单")
        base = ("题", [], "B", "")
        p1 = tp.build_review_prompt(*base, d, "记录", True)
        p2 = tp.build_review_prompt(*base, d, "记录", False)
        self.assertIn("逐条回应", p1)
        self.assertNotIn("逐条回应", p2)
        for needle in (d.version_id, "草案文", "分歧点清单", tp.MARKER_VERDICT,
                       "硬伤", "残余风险声明", "一次性列全"):
            self.assertIn(needle, p1)

    def test_revision_prompt_lists_blockers(self):
        d = tp.Draft(1, "草案文", "A", "log")
        p = tp.build_revision_prompt(
            "题", [], "B", "", d, [("C", tp.Blocker("硬伤", "缺X"))], "记录"
        )
        self.assertIn("（C）[硬伤] 缺X", p)
        self.assertIn(tp.MARKER_DRAFT, p)
        self.assertIn(tp.MARKER_CHANGELOG, p)
        self.assertIn("完整的", p)

    def test_confirm_prompt_addresses_author(self):
        d = tp.Draft(2, "文", "A", "log")
        p = tp.build_confirm_prompt("题", [], "A", "", d, "记录")
        self.assertIn("生成不等于审查", p)
        self.assertIn(d.version_id, p)

    def test_recommendation_prompt_handles_no_draft(self):
        p = tp.build_recommendation_prompt("题", [], "A", "", None, [], "记录")
        self.assertIn("成稿前终止", p)
        self.assertIn("未经全员认可", p)

    def test_render_transcript_truncates_oldest_cycles(self):
        events = [
            tp.Event("proposal", 0, "A", "早期长文" * 5000),
            tp.Event("review", 1, "B", "中期" * 10),
            tp.Event("review", 2, "C", "最新发言"),
        ]
        out = tp.render_transcript(events, max_chars=500)
        self.assertIn("（阶段0发言已省略）", out)
        self.assertIn("最新发言", out)
        self.assertNotIn("早期长文", out)
        full = tp.render_transcript(events, max_chars=10_000_000)
        self.assertIn("早期长文", full)

    def test_render_transcript_always_keeps_newest_cycle(self):
        events = [tp.Event("review", 1, "B", "唯一发言" * 1000)]
        out = tp.render_transcript(events, max_chars=10)
        self.assertIn("唯一发言", out)

    def test_render_events_md(self):
        md = tp.render_events_md([tp.Event("review", 2, "B", "正文")])
        self.assertIn("## [周期2] B", md)
        self.assertIn("正文", md)
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_protocol -v`
Expected: FAIL（缺 `sanitize_slug` 等）

- [ ] **Step 3: 实现**（追加到 `table_protocol.py`）

```python
def sanitize_slug(topic: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"[^\w-]+", "-", topic).strip("-_")
    return cleaned[:max_len].strip("-_") or "untitled"


_FMT_VERDICT = f"""你的输出必须是：先写公开论述，然后独占一行写 {MARKER_VERDICT}，其后是表态：
- 接受当前草案：首行为 ACCEPT，随后必须附「残余风险声明」——当前方案最强的反例或失败边界，
  以及你为何认为这些剩余分歧可以容忍。"感觉合理"不构成 ACCEPT。
- 反对当前草案：首行为 BLOCK，随后一次性列全你发现的全部问题（不得只报告一部分留待下轮），
  每行一条，格式：
  - [硬伤] <描述>
  - [偏好] <描述>
  - [待验证] <描述>"""

_FMT_EDITOR = f"""你的输出必须依次包含三段：
1. 你的公开说明；
2. 独占一行的 {MARKER_DRAFT}，其后是**完整的**新版草案（自包含，不得写"同上"或引用旧版）；
3. 独占一行的 {MARKER_CHANGELOG}，其后是变更清单。"""


def _preamble(name: str, lens: str, role: str, topic: str, constraints: list[str]) -> str:
    lens_line = f"\n你的审查侧重：{lens}" if lens else ""
    cons = "\n".join(f"H{i + 1}. {c}" for i, c in enumerate(constraints)) or "（暂无）"
    return f"""你是多 AI 圆桌讨论的参与者「{name}」。{role}{lens_line}

讨论规则：这是一场对抗性但求真的技术讨论。直接、具体、给论据；不客套，也不为反对而反对。
人类插话是最高优先级的绑定约束，最终方案必须满足所有人类约束。
当前人类约束：
{cons}

议题：
{topic}"""


def build_proposal_prompt(topic: str, constraints: list[str], name: str, lens: str) -> str:
    return f"""{_preamble(name, lens, "现在是独立立论阶段：你看不到其他参与者的输出。", topic, constraints)}

请独立完成你对议题的分析，并给出你的完整方案。
输出格式：先写分析，然后独占一行写 {MARKER_PROPOSAL}，其后是你的完整方案。"""


def build_merge_prompt(topic: str, constraints: list[str], name: str, lens: str,
                       proposals: dict[str, str]) -> str:
    merged = "\n\n".join(f"《{p} 的独立方案》\n{t}" for p, t in proposals.items())
    return f"""{_preamble(name, lens, "你是本周期的轮值主编。", topic, constraints)}

以下是各参与者互不可见地提交的独立方案：

{merged}

请把它们合成为草案 v1：吸收各方案的合理部分，并在变更清单位置**明确列出各方案之间的分歧点**
（这是后续辩论的靶子，不要抹平分歧）。
{_FMT_EDITOR}"""


def build_review_prompt(topic: str, constraints: list[str], name: str, lens: str,
                        draft: Draft, transcript: str, first_cycle: bool) -> str:
    extra = ("\n本周期是第 1 评审周期：若你选择 ACCEPT，必须逐条回应变更清单中列出的每一个分歧点。"
             if first_cycle else "")
    return f"""{_preamble(name, lens, "你是当前版本的评审者。同周期其他评审者的发言对你不可见。", topic, constraints)}

已公开的讨论记录：
{transcript}

当前草案 {draft.version_id}（作者：{draft.author}）：
{draft.text}

上一版变更清单：
{draft.changelog}

请评审当前草案。{extra}
{_FMT_VERDICT}"""


def build_confirm_prompt(topic: str, constraints: list[str], name: str, lens: str,
                         draft: Draft, transcript: str) -> str:
    return f"""{_preamble(name, lens,
        "全体评审者已接受你起草的当前版本。生成不等于审查：请以评审者身份重新审读你自己的草案原文，做最后确认投票。",
        topic, constraints)}

已公开的讨论记录：
{transcript}

待确认草案 {draft.version_id}（作者：你）：
{draft.text}

{_FMT_VERDICT}"""


def build_revision_prompt(topic: str, constraints: list[str], name: str, lens: str,
                          draft: Draft, blockers: list[tuple[str, Blocker]],
                          transcript: str) -> str:
    if blockers:
        blk = "\n".join(f"- （{p}）[{b.severity}] {b.text}" for p, b in blockers)
    else:
        blk = "（本轮无 BLOCK，修订由新的人类约束触发）"
    return f"""{_preamble(name, lens, "你是本周期的轮值主编。", topic, constraints)}

已公开的讨论记录：
{transcript}

当前草案 {draft.version_id}（作者：{draft.author}）：
{draft.text}

待处理的全部 blocker：
{blk}

请产出下一版草案。变更清单必须逐条说明对每个 blocker（及每条新的人类约束）的处理：
采纳 / 部分采纳 / 拒绝并说明理由。
{_FMT_EDITOR}"""


def build_recommendation_prompt(topic: str, constraints: list[str], name: str, lens: str,
                                draft: Draft | None, blockers: list[tuple[str, Blocker]],
                                transcript: str) -> str:
    cur = (f"当前候选草案 {draft.version_id}：\n{draft.text}"
           if draft else "（讨论在成稿前终止，无候选草案）")
    blk = "\n".join(f"- （{p}）[{b.severity}] {b.text}" for p, b in blockers) or "（无）"
    return f"""{_preamble(name, lens,
        "讨论未达成共识而终止。你被指定给出个人建议——它将被明确标注为「主编个人建议，未经全员认可」。",
        topic, constraints)}

已公开的讨论记录：
{transcript}

{cur}

仍然有效的 blocker：
{blk}

请给出你的最终建议方案（自由文本，无需标记），并说明你如何权衡未决分歧。"""


def render_transcript(events: list[Event], max_chars: int = 120_000) -> str:
    """按周期渲染讨论记录；超长时从最早周期起丢弃正文，保留占位行；至少保留最新周期。"""
    if not events:
        return "（暂无）"
    cycles = sorted({e.cycle for e in events})
    text = ""
    for dropped in range(len(cycles)):
        parts: list[str] = []
        for c in cycles[:dropped]:
            parts.append("（阶段0发言已省略）" if c == 0 else f"（第 {c} 周期讨论已省略）")
        for c in cycles[dropped:]:
            parts.extend(f"【{e.participant}】\n{e.text}" for e in events if e.cycle == c)
        text = "\n\n".join(parts)
        if len(text) <= max_chars:
            return text
    return text


def render_events_md(events: list[Event]) -> str:
    parts = []
    for e in events:
        head = "阶段0" if e.cycle == 0 else f"周期{e.cycle}"
        parts.append(f"\n## [{head}] {e.participant}\n\n{e.text}\n")
    return "".join(parts)
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_protocol -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add table_protocol.py tests/test_protocol.py
git commit -m "feat: prompt 模板、讨论记录渲染与截断保护、slug 清洗"
```

---

### Task 5: final.md 渲染

**Files:**
- Modify: `table_protocol.py`（追加）
- Test: `tests/test_protocol.py`（追加）

**Interfaces:**
- Consumes: Task 2-3 的 `Discussion/Result/Verdict`
- Produces: `render_final(disc: Discussion, recommendation: str | None) -> str`

- [ ] **Step 1: 写失败测试**（追加）

```python
class TestRenderFinal(unittest.TestCase):
    ACCEPT = tp.ParsedVerdict(tp.Verdict.ACCEPT, "最强反例X；可容忍")

    def test_consensus_document(self):
        disc = tp.Discussion("题", ["A", "B"], 5)
        disc.add_draft("A", "最终方案原文", "log")
        disc.record_vote("B", self.ACCEPT, "s")
        disc.record_vote("A", self.ACCEPT, "s")
        doc = tp.render_final(disc, None)
        self.assertIn("CONSENSUS", doc)
        self.assertIn("最终方案原文", doc)
        self.assertIn("未经任何事后润色", doc)
        self.assertIn("最强反例X", doc)
        self.assertIn("模型共识不等于方案正确", doc)
        self.assertNotIn("主编个人建议", doc)

    def test_no_consensus_document_with_recommendation(self):
        disc = tp.Discussion("题", ["A", "B"], 5)
        disc.add_draft("A", "候选文", "log")
        disc.record_vote("B", tp.ParsedVerdict(
            tp.Verdict.BLOCK, "raw",
            (tp.Blocker("硬伤", "缺X"), tp.Blocker("待验证", "QPS假设")),
        ), "s")
        doc = tp.render_final(disc, "我建议这样折中")
        self.assertIn("NO_CONSENSUS", doc)
        self.assertIn("候选文", doc)
        self.assertIn("[硬伤]", doc)
        self.assertIn("缺X", doc)
        self.assertIn("QPS假设", doc)          # 共同盲区聚合待验证项
        self.assertIn("主编个人建议（未经全员认可", doc)
        self.assertIn("我建议这样折中", doc)

    def test_incomplete_before_draft(self):
        disc = tp.Discussion("题", ["A", "B"], 5)
        doc = tp.render_final(disc, None)
        self.assertIn("INCOMPLETE", doc)
        self.assertIn("形成任何草案之前终止", doc)

    def test_pending_facts_deduplicated_in_blind_spot_section(self):
        disc = tp.Discussion("题", ["A", "B"], 5)
        disc.add_draft("A", "v1", "l")
        blk = tp.ParsedVerdict(tp.Verdict.BLOCK, "raw", (tp.Blocker("待验证", "同一假设"),))
        disc.record_vote("B", blk, "s")
        disc.add_draft("B", "v2", "l")
        disc.record_vote("A", blk, "s")
        doc = tp.render_final(disc, None)
        blind_section = doc.split("共同盲区")[1]
        self.assertEqual(blind_section.count("同一假设"), 1)  # vote_log 中出现两次，盲区去重为一条
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_protocol -v`
Expected: FAIL（缺 `render_final`）

- [ ] **Step 3: 实现**（追加到 `table_protocol.py`）

```python
def render_final(disc: Discussion, recommendation: str | None) -> str:
    result = disc.outcome()
    lines = [
        f"# 圆桌结果：{result.value}", "",
        f"**议题：** {disc.topic}", "",
        f"**参与者：** {', '.join(disc.participants)} · 评审周期 {disc.cycle}/{disc.max_rounds}", "",
    ]
    if disc.constraints:
        lines += ["**人类约束：**"]
        lines += [f"- H{i + 1}. {c}" for i, c in enumerate(disc.constraints)]
        lines += [""]
    cur = disc.current
    if cur is None:
        lines += ["讨论在形成任何草案之前终止。", ""]
    elif result is Result.CONSENSUS:
        lines += [
            f"## 最终方案（{cur.version_id} · 全员表态接受的原文，未经任何事后润色）", "",
            cur.text, "",
            "## 表态记录", "",
        ]
        for p in disc.participants:
            v = disc.valid_vote(p)
            lines.append(f"- **{p}** → {v.version_id} ACCEPT")
            lines.append(f"  - 残余风险声明：{v.statement}")
        lines += ["", "> 本记录仅证明：以上无状态 CLI 调用对该确切文本返回了 ACCEPT。",
                  "> 模型共识不等于方案正确。", ""]
    else:
        lines += [
            f"## 当前候选草案（{cur.version_id} · 作者 {cur.author} · 未达成共识）", "",
            cur.text, "",
            "## 仍然有效的 blocker", "",
        ]
        blockers = disc.active_blockers()
        if blockers:
            for sev in ("硬伤", "偏好", "待验证"):
                group = [(p, b) for p, b in blockers if b.severity == sev]
                if group:
                    lines.append(f"**[{sev}]**")
                    lines += [f"- （{p}）{b.text}" for p, b in group]
        else:
            lines.append("（无有效 BLOCK——未达共识源于缺票或人工提前终止）")
        lines += ["", "## 各方最后立场", ""]
        for p in disc.participants:
            v = disc.votes.get(p)
            if v is None:
                lines.append(f"- **{p}**：本版本未表态")
            else:
                lines.append(f"- **{p}**：{v.verdict.value}（{v.version_id}）— {v.statement[:200]}")
    seen: set[str] = set()
    pending = [
        (v.participant, b) for v in disc.vote_log for b in v.blockers
        if b.severity == "待验证" and not (b.text in seen or seen.add(b.text))
    ]
    lines += ["", "## 共同盲区与外部验证建议", ""]
    if pending:
        lines += [f"- （{p}）{b.text}" for p, b in pending]
    else:
        lines.append("- 讨论中未显式标记待验证事实。")
    lines += ["", "> 三个模型可能共享同一种错误知识；关键决策请在模型之外验证。", ""]
    if recommendation:
        lines += ["## 附录：主编个人建议（未经全员认可，不构成共识）", "", recommendation, ""]
    return "\n".join(lines)
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_protocol -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add table_protocol.py tests/test_protocol.py
git commit -m "feat: final.md 渲染（共识/无共识/不完整三种终局）"
```

---

### Task 6: 参与者配置加载

**Files:**
- Create: `table.py`, `tests/test_table.py`

**Interfaces:**
- Consumes: 无（table.py 起点）
- Produces: `ParticipantConfig(name, cmd: tuple[str, ...], lens="", timeout=300)`（frozen dataclass）；`DEFAULT_PARTICIPANTS: list[ParticipantConfig]`；`load_config(path: pathlib.Path | None) -> list[ParticipantConfig]`（path=None 时自动找 `./table.toml`，没有则内置默认；显式 path 不存在 → SystemExit）

- [ ] **Step 1: 写失败测试**

`tests/test_table.py`：

```python
import contextlib
import pathlib
import tempfile
import unittest

import table


class TestConfig(unittest.TestCase):
    def test_defaults_when_no_file(self):
        with tempfile.TemporaryDirectory() as td, contextlib.chdir(td):
            pcs = table.load_config(None)
        self.assertEqual([p.name for p in pcs], ["Claude", "Codex", "Gemini"])
        self.assertEqual(pcs[0].cmd, ("claude", "-p"))
        self.assertEqual(pcs[0].timeout, 300)

    def test_load_toml(self):
        with tempfile.TemporaryDirectory() as td:
            f = pathlib.Path(td) / "t.toml"
            f.write_text(
                '[[participants]]\nname = "X"\ncmd = ["echo", "hi"]\nlens = "成本"\ntimeout = 7\n'
                '[[participants]]\nname = "Y"\ncmd = ["cat"]\n',
                encoding="utf-8",
            )
            pcs = table.load_config(f)
        self.assertEqual(pcs[0], table.ParticipantConfig("X", ("echo", "hi"), "成本", 7))
        self.assertEqual(pcs[1].lens, "")

    def test_validation_errors(self):
        cases = [
            '[[participants]]\nname = "X"\ncmd = ["a"]\n',                       # 少于2人
            '[[participants]]\nname = "X"\ncmd = []\n[[participants]]\nname = "Y"\ncmd = ["a"]\n',
            '[[participants]]\nname = "X"\ncmd = ["a"]\n[[participants]]\nname = "X"\ncmd = ["b"]\n',
        ]
        for content in cases:
            with tempfile.TemporaryDirectory() as td:
                f = pathlib.Path(td) / "t.toml"
                f.write_text(content, encoding="utf-8")
                with self.assertRaises(SystemExit):
                    table.load_config(f)

    def test_explicit_missing_path_exits(self):
        with self.assertRaises(SystemExit):
            table.load_config(pathlib.Path("/nonexistent/t.toml"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_table -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'table'`）

- [ ] **Step 3: 实现**

`table.py`：

```python
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
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_table -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add table.py tests/test_table.py
git commit -m "feat: 参与者配置（内置默认 + table.toml 覆盖 + 校验）"
```

---

### Task 7: CLI 适配器与假 CLI

**Files:**
- Modify: `table.py`（追加）
- Create: `tests/fake_cli.py`
- Test: `tests/test_table.py`（追加）

**Interfaces:**
- Consumes: Task 6 的 `ParticipantConfig`
- Produces: `CliError(Exception)`；`call_cli(pc, prompt, cwd, audit, purpose) -> str`（audit 是 `Callable[[dict], None]`；超时/失败自动重试一次，进程组击杀，失败抛 CliError）；`call_and_parse(pc, prompt, parser, cwd, audit, purpose) -> tuple[Any | None, str]`（一次格式纠错重试；返回 (解析结果或 None, 最后原文)）
- Produces（测试侧）: `tests/fake_cli.py`——用法 `fake_cli.py <scenario_dir> <role>`；按 `<scenario_dir>/<role>/001.txt…` 顺序吐响应；prompt 存档到 `<scenario_dir>/calls/<role>-<n>-prompt.txt`；响应首行特殊指令 `SLEEP <secs>`（写 pid 文件后挂起）、`BARRIER <path>`（touch 后写 pid 挂起 600s）、`BIGERR`（1MB stderr + 其余行作 stdout）

- [ ] **Step 1: 写假 CLI 与失败测试**

`tests/fake_cli.py`：

```python
#!/usr/bin/env python3
"""剧本驱动的假 CLI。用法: fake_cli.py <scenario_dir> <role>

剧本: <scenario_dir>/<role>/ 下的 001.txt, 002.txt …（按调用次序消费，.counter 计数）。
行为: 读 stdin 作为 prompt，存档到 <scenario_dir>/calls/<role>-<n>-prompt.txt，打印响应文件内容。
响应文件首行特殊指令:
  SLEEP <secs>   → 写 <role>-<n>.pid 后挂起（超时测试）
  BARRIER <path> → touch <path>、写 pid 后挂起 600s（kill 持久化测试）
  BIGERR         → 向 stderr 写 1MB，再把其余行写到 stdout
"""
import os
import pathlib
import sys
import time


def main() -> None:
    scenario = pathlib.Path(sys.argv[1])
    role = sys.argv[2]
    prompt = sys.stdin.read()
    d = scenario / role
    counter = d / ".counter"
    n = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(n))
    calls = scenario / "calls"
    calls.mkdir(exist_ok=True)
    (calls / f"{role}-{n}-prompt.txt").write_text(prompt, encoding="utf-8")
    resp_file = d / f"{n:03d}.txt"
    if not resp_file.exists():
        print(f"fake_cli: {role} 没有第 {n} 号响应", file=sys.stderr)
        sys.exit(3)
    resp = resp_file.read_text(encoding="utf-8")
    first = resp.splitlines()[0] if resp else ""
    if first.startswith("SLEEP "):
        (calls / f"{role}-{n}.pid").write_text(str(os.getpid()))
        time.sleep(float(first.split()[1]))
        sys.exit(0)
    if first.startswith("BARRIER "):
        pathlib.Path(first.split(maxsplit=1)[1]).touch()
        (calls / f"{role}-{n}.pid").write_text(str(os.getpid()))
        time.sleep(600)
        sys.exit(0)
    if first == "BIGERR":
        sys.stderr.write("x" * 1_000_000)
        sys.stdout.write(resp.partition("\n")[2])
        sys.exit(0)
    sys.stdout.write(resp)


if __name__ == "__main__":
    main()
```

追加到 `tests/test_table.py`：

```python
import os
import sys

FAKE = pathlib.Path(__file__).parent / "fake_cli.py"


def fake_pc(name, scenario, timeout=30):
    return table.ParticipantConfig(
        name, (sys.executable, str(FAKE), str(scenario), name), "", timeout
    )


def write_script(scenario, role, responses):
    d = pathlib.Path(scenario) / role
    d.mkdir(parents=True, exist_ok=True)
    for i, r in enumerate(responses, 1):
        (d / f"{i:03d}.txt").write_text(r, encoding="utf-8")


class TestAdapter(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._td.name)
        self.scenario = self.base / "s"
        self.audit_log = []
        self.audit = self.audit_log.append

    def tearDown(self):
        self._td.cleanup()

    def test_call_cli_ok_and_prompt_via_stdin(self):
        write_script(self.scenario, "X", ["回复内容"])
        out = table.call_cli(fake_pc("X", self.scenario), "问题", self.base, self.audit, "t")
        self.assertEqual(out, "回复内容")
        self.assertEqual(
            (self.scenario / "calls" / "X-1-prompt.txt").read_text(encoding="utf-8"), "问题"
        )
        self.assertEqual(self.audit_log[0]["outcome"], "ok")

    def test_call_cli_timeout_kills_group_and_retries(self):
        write_script(self.scenario, "X", ["SLEEP 30", "SLEEP 30"])
        pc = fake_pc("X", self.scenario, timeout=2)
        with self.assertRaises(table.CliError):
            table.call_cli(pc, "q", self.base, self.audit, "t")
        self.assertEqual([r["outcome"] for r in self.audit_log], ["timeout", "timeout"])
        for n in (1, 2):
            pid = int((self.scenario / "calls" / f"X-{n}.pid").read_text())
            with self.assertRaises(OSError):
                os.kill(pid, 0)  # 进程组已被击杀

    def test_call_cli_retry_then_success(self):
        write_script(self.scenario, "X", ["SLEEP 30", "第二次成功"])
        pc = fake_pc("X", self.scenario, timeout=2)
        out = table.call_cli(pc, "q", self.base, self.audit, "t")
        self.assertEqual(out, "第二次成功")

    def test_call_cli_missing_binary(self):
        pc = table.ParticipantConfig("X", ("definitely-missing-binary-xyz",))
        with self.assertRaises(table.CliError):
            table.call_cli(pc, "q", self.base, self.audit, "t")

    def test_call_cli_truncates_huge_stderr_in_audit(self):
        write_script(self.scenario, "X", ["BIGERR\n正文"])
        out = table.call_cli(fake_pc("X", self.scenario), "q", self.base, self.audit, "t")
        self.assertEqual(out, "正文")
        self.assertLessEqual(len(self.audit_log[0]["stderr_tail"]), 500)

    def test_call_and_parse_format_retry(self):
        write_script(self.scenario, "X", ["没有标记", "论述\n---VERDICT---\nACCEPT: 理由"])
        parsed, raw = table.call_and_parse(
            fake_pc("X", self.scenario), "q", tp.parse_verdict, self.base, self.audit, "t"
        )
        self.assertIsNotNone(parsed)
        second_prompt = (self.scenario / "calls" / "X-2-prompt.txt").read_text(encoding="utf-8")
        self.assertIn("格式纠错", second_prompt)
        self.assertIn("没有标记", second_prompt)

    def test_call_and_parse_double_failure_returns_none(self):
        write_script(self.scenario, "X", ["坏1", "坏2"])
        parsed, raw = table.call_and_parse(
            fake_pc("X", self.scenario), "q", tp.parse_verdict, self.base, self.audit, "t"
        )
        self.assertIsNone(parsed)
        self.assertEqual(raw, "坏2")
```

并在 `tests/test_table.py` 顶部补充导入：`import table_protocol as tp`。

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_table -v`
Expected: FAIL（缺 `call_cli`）

- [ ] **Step 3: 实现**（追加到 `table.py`）

```python
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
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_table -v`
Expected: 全部 PASS（超时用例约需 4-5 秒）

- [ ] **Step 5: 提交**

```bash
git add table.py tests/fake_cli.py tests/test_table.py
git commit -m "feat: CLI 适配器（进程组超时击杀/重试/审计/格式纠错）与假 CLI"
```

---

### Task 8: RunStore 落盘

**Files:**
- Modify: `table.py`（追加）
- Test: `tests/test_table.py`（追加）

**Interfaces:**
- Consumes: Task 1-5 的 `tp.sanitize_slug/render_events_md/snapshot`；Task 2 的 `Discussion`
- Produces: `RunStore(base: pathlib.Path, topic: str)`，属性 `dir`、`sandbox`；方法 `transcript(disc)`（增量追加新事件 + fsync）、`state(disc)`（tmp + os.replace 原子写）、`audit(rec: dict)`（jsonl 追加 + fsync）、`raw(name, text)`（存档到 `raw/`）、`final(text)`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_table.py`）

```python
class TestRunStore(unittest.TestCase):
    def test_dir_naming_and_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            s1 = table.RunStore(base, "议题/名？")
            s2 = table.RunStore(base, "议题/名？")
            self.assertTrue(s1.sandbox.is_dir())
            self.assertNotEqual(s1.dir, s2.dir)
            self.assertNotIn("/", s1.dir.name.replace(str(base), ""))
            self.assertIn("议题-名", s1.dir.name)

    def test_incremental_transcript_and_atomic_state(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            store = table.RunStore(base, "题")
            disc = tp.Discussion("题", ["A", "B"], 5)
            disc.add_proposal("A", "分析", "方案")
            store.transcript(disc)
            store.state(disc)
            disc.add_draft("A", "v1", "log")
            store.transcript(disc)
            text = (store.dir / "transcript.md").read_text(encoding="utf-8")
            self.assertEqual(text.count("[阶段0] A"), 2)  # proposal + draft 事件各一次，无重复
            snap = json.loads((store.dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(snap["topic"], "题")

    def test_audit_raw_final(self):
        with tempfile.TemporaryDirectory() as td:
            store = table.RunStore(pathlib.Path(td), "题")
            store.audit({"a": 1})
            store.audit({"b": "中文"})
            store.raw("X", "坏输出")
            store.final("终局")
            lines = (store.dir / "session.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("中文", lines[1])
            self.assertEqual(len(list((store.dir / "raw").iterdir())), 1)
            self.assertEqual((store.dir / "final.md").read_text(encoding="utf-8"), "终局")
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_table -v`
Expected: FAIL（缺 `RunStore`）

- [ ] **Step 3: 实现**（追加到 `table.py`）

```python
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
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_table -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add table.py tests/test_table.py
git commit -m "feat: RunStore 事件级落盘（增量 transcript/原子 state/审计/存档）"
```

---

### Task 9: 编排主循环与终端输出

**Files:**
- Modify: `table.py`（追加）
- Create: `tests/test_integration.py`

**Interfaces:**
- Consumes: 前述全部——`tp.Discussion` 全套、`call_cli/call_and_parse/RunStore/ParticipantConfig`
- Produces: `PALETTE/RESET/BOLD/DIM` 常量、`USE_COLOR`、`say(name, color, header, body)`、`say_system(msg)`；`run(topic, pcs, max_rounds, max_context_chars, runs_dir, intervention=None, do_preflight=True) -> int`（intervention 是 `Callable[[], str | None]`，None 用 Task 10 的 `default_intervention`；返回 0=正常终局，1=中止）。Task 10 消费 `run` 与 `say_system`。

- [ ] **Step 1: 写失败测试**

`tests/test_integration.py`：

```python
import json
import pathlib
import sys
import tempfile
import unittest

import table
import table_protocol as tp

FAKE = pathlib.Path(__file__).parent / "fake_cli.py"


def fake_pc(name, scenario, timeout=30):
    return table.ParticipantConfig(
        name, (sys.executable, str(FAKE), str(scenario), name), "", timeout
    )


def write_script(scenario, role, responses):
    d = pathlib.Path(scenario) / role
    d.mkdir(parents=True, exist_ok=True)
    for i, r in enumerate(responses, 1):
        (d / f"{i:03d}.txt").write_text(r, encoding="utf-8")


def proposal(text):
    return f"我的分析\n---PROPOSAL---\n{text}"


def editor_out(draft, log):
    return f"主编说明\n---DRAFT---\n{draft}\n---CHANGELOG---\n{log}"


def accept(stmt):
    return f"评审论述\n---VERDICT---\nACCEPT: {stmt}"


def block(*items):
    body = "\n".join(f"- [{s}] {t}" for s, t in items)
    return f"评审论述\n---VERDICT---\nBLOCK\n{body}"


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        base = pathlib.Path(self._td.name)
        self.scenario = base / "s"
        self.runs = base / "runs"

    def tearDown(self):
        self._td.cleanup()

    def run_table(self, pcs, max_rounds=3, intervention=lambda: None):
        return table.run("测试议题", pcs, max_rounds, 120_000, self.runs,
                         intervention=intervention, do_preflight=False)

    def run_dir(self):
        return next(self.runs.iterdir())

    def read(self, name):
        return (self.run_dir() / name).read_text(encoding="utf-8")


class TestHappyPath(Base):
    def test_three_way_consensus_first_cycle(self):
        write_script(self.scenario, "A", [
            proposal("先单体"),
            editor_out("方案：先单体后拆分", "分歧点：\n- 是否上微服务"),
            accept("反例：团队到20人要重构；当前5人可容忍"),
        ])
        write_script(self.scenario, "B", [
            proposal("上微服务"),
            accept("反例：运维成本被低估；草案含演进路径，可容忍"),
        ])
        write_script(self.scenario, "C", [
            proposal("看情况"),
            accept("反例：拆分时机模糊；已列触发条件，可容忍"),
        ])
        code = self.run_table([fake_pc(n, self.scenario) for n in "ABC"])
        self.assertEqual(code, 0)
        final = self.read("final.md")
        self.assertIn("CONSENSUS", final)
        self.assertIn("先单体后拆分", final)
        state = json.loads(self.read("state.json"))
        self.assertEqual(state["outcome"], "CONSENSUS")
        self.assertEqual(len(state["drafts"]), 1)
        transcript = self.read("transcript.md")
        for needle in ("先单体", "上微服务", "看情况", "运维成本被低估"):
            self.assertIn(needle, transcript)
        self.assertGreaterEqual(
            len(self.read("session.jsonl").splitlines()), 6
        )  # 3提案+1合成+2评审+1确认

    def test_same_cycle_reviews_are_blind(self):
        write_script(self.scenario, "A", [
            proposal("pa"), editor_out("v1文", "分歧"), accept("确认理由：反例X可容忍"),
        ])
        write_script(self.scenario, "B", [proposal("pb"), accept("B独特理由XYZZY")])
        write_script(self.scenario, "C", [proposal("pc"), accept("C独特理由PLUGH")])
        self.run_table([fake_pc(n, self.scenario) for n in "ABC"])
        b_review_prompt = (self.scenario / "calls" / "B-2-prompt.txt").read_text(encoding="utf-8")
        c_review_prompt = (self.scenario / "calls" / "C-2-prompt.txt").read_text(encoding="utf-8")
        self.assertNotIn("C独特理由PLUGH", b_review_prompt)   # 同周期互不可见
        self.assertNotIn("B独特理由XYZZY", c_review_prompt)
        self.assertIn("pb", c_review_prompt)                  # 历史周期（阶段0）公开


class TestBlockRevision(Base):
    def test_block_then_revise_then_consensus(self):
        write_script(self.scenario, "A", [
            proposal("pa"), editor_out("v1文", "分歧"), accept("反例可容忍"),
        ])
        write_script(self.scenario, "B", [
            proposal("pb"), block(("硬伤", "缺一致性"), ("待验证", "QPS假设")),
            editor_out("v2文修复一致性", "处理：\n- 采纳B的硬伤"),
            accept("作者确认：反例已收敛"),
        ])
        write_script(self.scenario, "C", [
            proposal("pc"), accept("v1可以"), accept("v2也可以"),
        ])
        code = self.run_table([fake_pc(n, self.scenario) for n in "ABC"])
        self.assertEqual(code, 0)
        state = json.loads(self.read("state.json"))
        self.assertEqual(state["outcome"], "CONSENSUS")
        self.assertEqual(len(state["drafts"]), 2)
        self.assertEqual(state["drafts"][1]["author"], "B")   # 轮值主编 = A 的下一位
        final = self.read("final.md")
        self.assertIn("v2文修复一致性", final)
        self.assertIn("QPS假设", final)                        # 待验证进共同盲区
        names = [v["participant"] for v in state["vote_log"]]
        self.assertEqual(names.count("C"), 2)                  # v1、v2 各投一次


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_integration -v`
Expected: FAIL（`AttributeError: module 'table' has no attribute 'run'`）

- [ ] **Step 3: 实现**（追加到 `table.py`）

```python
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
```

同时在 `table.py` 中先补一个最小占位（Task 10 才实现真身，本任务测试全部注入 intervention，不会触发）：

```python
def default_intervention() -> str | None:
    return None


def preflight(pcs, store) -> None:
    raise NotImplementedError  # Task 10 实现
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_integration -v` 以及回归 `python3 -m unittest discover -s tests -t . -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add table.py tests/test_integration.py
git commit -m "feat: 编排主循环（阶段0/合成/盲审/确认/修订/终局）与终端输出"
```

---

### Task 10: 插话、预检与主入口

**Files:**
- Modify: `table.py`（替换 Task 9 的两个占位 + 追加 `main`）
- Test: `tests/test_integration.py`（追加）、`tests/test_table.py`（追加 preflight 用例）

**Interfaces:**
- Consumes: Task 9 的 `run/say_system`、Task 7 的 `call_cli/CliError`
- Produces: `default_intervention() -> str | None`（非 TTY 返回 None；TTY 下 select 非阻塞检测 Enter，交互读入；空回车 → None）；`preflight(pcs, store)`（每人 60s ping，失败 SystemExit）；`scripted(responses)`（仅测试侧 helper）；`main(argv=None) -> int`（argparse：`topic` 位置参数、`--max-rounds` 默认 5、`--config`、`--max-context-chars` 默认 120000、`--runs-dir` 默认 `runs`、`--skip-preflight`；KeyboardInterrupt → 提示已落盘，返回 130）；`table.py` 末尾 `if __name__ == "__main__": sys.exit(main())`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_integration.py`：

```python
def scripted(responses):
    it = iter(responses)
    return lambda: next(it, None)


class TestInterjection(Base):
    def test_constraint_voids_votes_and_reaches_editor(self):
        # N=2：v1 作者 A、评审 B；周期1评审后插话 → B 的票作废 → B 修订 v2 → A 评审 → B 确认
        write_script(self.scenario, "A", [
            proposal("pa"), editor_out("v1文", "分歧"), accept("v2可以"),
        ])
        write_script(self.scenario, "B", [
            proposal("pb"), accept("v1可以但会被作废"),
            editor_out("v2文用Python", "处理：\n- 响应H1"),
            accept("作者确认"),
        ])
        pcs = [fake_pc(n, self.scenario) for n in "AB"]
        # checkpoint 顺序：合成前(None) → 周期1评审后(插话) → 修订后(None)…
        code = self.run_table(pcs, intervention=scripted([None, "必须用Python实现"]))
        self.assertEqual(code, 0)
        state = json.loads(self.read("state.json"))
        self.assertEqual(state["constraints"], ["必须用Python实现"])
        self.assertEqual(state["outcome"], "CONSENSUS")
        self.assertEqual(len(state["drafts"]), 2)
        revise_prompt = (self.scenario / "calls" / "B-3-prompt.txt").read_text(encoding="utf-8")
        self.assertIn("H1. 必须用Python实现", revise_prompt)
        review_v2_prompt = (self.scenario / "calls" / "A-3-prompt.txt").read_text(encoding="utf-8")
        self.assertIn("H1. 必须用Python实现", review_v2_prompt)
        b_votes = [v for v in state["vote_log"] if v["participant"] == "B"]
        self.assertEqual(len(b_votes), 2)  # v1票（插话后被作废，日志保留）+ v2作者确认票

    def test_stop_before_merge_yields_incomplete_with_recommendation(self):
        write_script(self.scenario, "A", [proposal("pa"), "我的个人建议全文"])
        write_script(self.scenario, "B", [proposal("pb")])
        pcs = [fake_pc(n, self.scenario) for n in "AB"]
        code = self.run_table(pcs, intervention=scripted(["/stop"]))
        self.assertEqual(code, 0)
        final = self.read("final.md")
        self.assertIn("INCOMPLETE", final)
        self.assertIn("形成任何草案之前终止", final)
        self.assertIn("主编个人建议", final)
        self.assertIn("我的个人建议全文", final)


class TestFailureSemantics(Base):
    def test_reviewer_timeout_then_makeup_poll(self):
        # B 评审两次超时（原调用+重试）→ 缺票 → 周期2补征成功 → 共识
        write_script(self.scenario, "A", [
            proposal("pa"), editor_out("v1文", "分歧"), accept("作者确认"),
        ])
        write_script(self.scenario, "B", [
            proposal("pb"), "SLEEP 30", "SLEEP 30", accept("补征时接受"),
        ])
        pcs = [fake_pc("A", self.scenario), fake_pc("B", self.scenario, timeout=2)]
        code = self.run_table(pcs, max_rounds=3)
        self.assertEqual(code, 0)
        state = json.loads(self.read("state.json"))
        self.assertEqual(state["outcome"], "CONSENSUS")
        audit = [json.loads(x) for x in self.read("session.jsonl").splitlines()]
        self.assertEqual(sum(1 for r in audit if r["outcome"] == "timeout"), 2)
        self.assertEqual(len(state["drafts"]), 1)  # 缺票不触发修订，只补征

    def test_end_to_end_argv_subprocess(self):
        import subprocess
        write_script(self.scenario, "A", [
            proposal("pa"), editor_out("v1文", "分歧"), accept("确认"),
        ])
        write_script(self.scenario, "B", [proposal("pb"), accept("同意")])
        cfg = self.scenario / "table.toml"
        cfg.write_text(
            "\n".join(
                f'[[participants]]\nname = "{n}"\n'
                f'cmd = ["{sys.executable}", "{FAKE}", "{self.scenario}", "{n}"]\n'
                for n in "AB"
            ),
            encoding="utf-8",
        )
        root = pathlib.Path(__file__).parent.parent
        proc = subprocess.run(
            [sys.executable, str(root / "table.py"), "端到端议题",
             "--config", str(cfg), "--runs-dir", str(self.runs),
             "--max-rounds", "2", "--skip-preflight"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.run_dir() / "final.md").exists())

    def test_kill9_leaves_consistent_files(self):
        import os
        import signal as sig
        import subprocess
        import time as t
        marker = self.scenario / "marker"
        write_script(self.scenario, "A", [proposal("pa"), editor_out("v1文", "分歧")])
        write_script(self.scenario, "B", [proposal("pb"), f"BARRIER {marker}"])
        cfg = self.scenario / "table.toml"
        cfg.write_text(
            "\n".join(
                f'[[participants]]\nname = "{n}"\n'
                f'cmd = ["{sys.executable}", "{FAKE}", "{self.scenario}", "{n}"]\n'
                'timeout = 600\n'
                for n in "AB"
            ),
            encoding="utf-8",
        )
        root = pathlib.Path(__file__).parent.parent
        proc = subprocess.Popen(
            [sys.executable, str(root / "table.py"), "崩溃议题",
             "--config", str(cfg), "--runs-dir", str(self.runs),
             "--max-rounds", "2", "--skip-preflight"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = t.monotonic() + 20
            while not marker.exists():
                self.assertLess(t.monotonic(), deadline, "BARRIER 未触发")
                t.sleep(0.2)
            os.killpg(proc.pid, sig.SIGKILL)
            proc.wait()
        finally:
            pid_file = self.scenario / "calls" / "B-2.pid"
            if pid_file.exists():
                with self.subTest("cleanup"):
                    try:
                        os.kill(int(pid_file.read_text()), sig.SIGKILL)
                    except OSError:
                        pass
        transcript = self.read("transcript.md")
        self.assertIn("pa", transcript)
        self.assertIn("v1文", transcript)          # 崩溃前事件均已落盘
        state = json.loads(self.read("state.json"))
        self.assertEqual(len(state["drafts"]), 1)
```

追加到 `tests/test_table.py`：

```python
class TestPreflight(unittest.TestCase):
    def test_pass_and_fail(self):
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            scenario = base / "s"
            write_script(scenario, "P", ["PONG"])
            store = table.RunStore(base / "runs", "题")
            table.preflight([fake_pc("P", scenario)], store)  # 不抛即通过
            bad = table.ParticipantConfig("Bad", ("definitely-missing-binary-xyz",))
            with self.assertRaises(SystemExit):
                table.preflight([bad], store)
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_integration tests.test_table -v`
Expected: FAIL（`preflight` 是 NotImplementedError 占位；main/入口缺失导致子进程用例失败）

- [ ] **Step 3: 实现**（替换 Task 9 的两个占位函数，并在文件末尾追加 `main`）

```python
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
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest discover -s tests -t . -v`
Expected: 全部 PASS（kill/超时用例合计约 10 秒）

- [ ] **Step 5: 提交**

```bash
git add table.py tests/test_integration.py tests/test_table.py
git commit -m "feat: 插话检查点、启动预检与 argparse 主入口"
```

---

### Task 11: README 与真实冒烟

**Files:**
- Create: `README.md`
- Create: `table.toml`（示例配置，与内置默认一致，便于用户改）

**Interfaces:**
- Consumes: 全部已完成功能

- [ ] **Step 1: 写 README.md**

```markdown
# Table —— 多 AI 圆桌辩论

让 claude / codex / gemini 就一个纯文本议题进行结构化对抗辩论，
产出一份**全员对同一版本文本明确表态接受**的方案；达不成共识则如实输出候选方案与分歧。

## 用法

​```bash
python3 table.py "微服务还是单体：5人团队的新电商项目怎么选？" --max-rounds 5
​```

- 讨论进行中按 **Enter** 可插话（人类约束，最高优先级，作废当前版本全部票）；`/stop` 提前收尾
- 结果落盘在 `runs/<时间>-<议题>/`：`transcript.md`（全记录）、`final.md`（终局）、
  `state.json`（协议状态）、`session.jsonl`（调用审计）

## 协议

独立立论（并行盲写）→ 轮值主编合成 v1（附分歧点）→ 各评审并行盲审（ACCEPT 附残余风险 /
BLOCK 一次列全分级问题）→ 主编修订（逐条处理）→ 循环。共识 = 全员（含作者确认票）对同一
`v{n}-{hash}` 版本 ACCEPT；新版本/人类插话作废全部旧票；缺票绝不计为同意。

## 配置

编辑 `table.toml`（不存在则用内置默认）：

​```toml
[[participants]]
name = "Claude"
cmd = ["claude", "-p"]        # prompt 走 stdin，回复取 stdout
lens = "重点审查：可维护性与长期复杂度"
timeout = 300
​```

## 注意

- 议题与全部讨论记录会发送给所有配置的模型供应商；输入 token 随轮数近似平方增长，
  `--max-rounds` 是主要成本阀门
- 模型共识 ≠ 事实正确；final.md 的「共同盲区」一节列出讨论中标记的待验证事实
- 要求 Python ≥ 3.11；零第三方依赖；测试：`python3 -m unittest discover -s tests -t .`
```

（写入文件时把 ​``` 换成正常三反引号。）

`table.toml`：内容为 spec §4 的三参与者示例（Claude/Codex/Gemini + lens + 注释）。

- [ ] **Step 2: 全量回归**

Run: `python3 -m unittest discover -s tests -t . -v`
Expected: 全部 PASS

- [ ] **Step 3: 真实冒烟（烧真 token，一次）**

```bash
python3 table.py "为一个5人团队的内部工具选择：SQLite还是PostgreSQL？给出决策标准" --max-rounds 2
```

检查：预检三个 CLI 通过；阶段0三份独立方案；v1 含分歧点；评审有实质表态；
`runs/<dir>/final.md` 结局合理（CONSENSUS 或 NO_CONSENSUS 均可接受，关键是流程与产物完整）。
若某 CLI 输出格式不达标触发大量 INVALID，调整对应 prompt 模板措辞后重试一次。

- [ ] **Step 4: 提交**

```bash
git add README.md table.toml
git commit -m "docs: README 与示例配置；真实冒烟通过"
```

---

## 计划自查记录

- **Spec 覆盖**：§3 形态/产物→T8/T10；§4 配置/lens/隔离/预检→T6/T7/T10；§5 协议全流程（盲写、合成、盲审、确认票、清票、补征、轮转）→T2/T3/T9；§6 插话语义→T3/T10；§7 三种终局+建议附录+共同盲区→T5/T9；§8 严格解析+一次纠错+INVALID→T1/T7；§9 拼装与截断→T4；§10 两模块结构→全局；§11 错误表→T7/T9/T10；§12 测试清单（含混合版本、插话作废、缺票、空草案、stderr、子进程残留、崩溃落盘）→各任务测试。
- **无占位符**：所有步骤含完整代码与确切命令。
- **类型一致性**：`speak` 返回 `(status, payload)` 三态贯穿 T9/T10；`parse_*` 返回元组形状与 T1 定义一致；`intervention: Callable[[], str | None]` 贯穿 T9/T10 与测试 helper `scripted`。
