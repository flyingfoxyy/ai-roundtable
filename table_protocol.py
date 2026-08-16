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


class Result(Enum):
    CONSENSUS = "CONSENSUS"
    NO_CONSENSUS = "NO_CONSENSUS"
    INCOMPLETE = "INCOMPLETE"


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
