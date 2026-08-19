"""Table 圆桌协议纯逻辑：解析、状态机、prompt 拼装、渲染。禁止任何 IO。"""
from __future__ import annotations

import difflib
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


def _vote_obj(d: dict) -> Vote:
    return Vote(d["participant"], d["version_id"], Verdict(d["verdict"]), d["statement"],
                tuple(Blocker(b["severity"], b["text"]) for b in d["blockers"]))


SNAPSHOT_FORMAT = 2  # 1 = 无 events/proposals 的早期格式，不可续会


def snapshot(disc: Discussion) -> dict:
    """state.json 用的可 JSON 化快照；完整到足以 restore() 出等价的 Discussion。"""
    return {
        "format": SNAPSHOT_FORMAT,
        "topic": disc.topic,
        "participants": disc.participants,
        "constraints": disc.constraints,
        "cycle": disc.cycle,
        "max_rounds": disc.max_rounds,
        "outcome": disc.outcome().value,
        "unaddressed_constraints": disc.unaddressed_constraints,
        "proposals": disc.proposals,
        "drafts": [
            {"number": d.number, "version_id": d.version_id, "author": d.author,
             "text": d.text, "changelog": d.changelog}
            for d in disc.drafts
        ],
        "votes": {p: _vote_dict(v) for p, v in disc.votes.items()},
        "vote_log": [_vote_dict(v) for v in disc.vote_log],
        "events": [
            {"kind": e.kind, "cycle": e.cycle, "participant": e.participant, "text": e.text}
            for e in disc.events
        ],
    }


def restore(snap: dict) -> Discussion:
    """从 snapshot() 的产物复原 Discussion；格式不符抛 ValueError。"""
    if snap.get("format") != SNAPSHOT_FORMAT:
        raise ValueError(
            f"快照格式不兼容（需要 format={SNAPSHOT_FORMAT}，实际 {snap.get('format')!r}）"
        )
    disc = Discussion(snap["topic"], snap["participants"], snap["max_rounds"])
    disc.constraints = list(snap["constraints"])
    disc.cycle = snap["cycle"]
    disc.unaddressed_constraints = snap["unaddressed_constraints"]
    disc.proposals = dict(snap["proposals"])
    disc.drafts = [Draft(d["number"], d["text"], d["author"], d["changelog"])
                   for d in snap["drafts"]]
    disc.votes = {p: _vote_obj(v) for p, v in snap["votes"].items()}
    disc.vote_log = [_vote_obj(v) for v in snap["vote_log"]]
    disc.events = [Event(e["kind"], e["cycle"], e["participant"], e["text"])
                   for e in snap["events"]]
    return disc


RECURRENCE_RATIO = 0.60  # 相似度提示阈值（实测标定：换说法重提 0.41~0.70，同领域不同问题 0.29~0.36，
                         # 两段重叠，故相似度只作弱提示，重提以评审显式声明为准）
SEVERITIES = ("硬伤", "偏好", "待验证")


@dataclass(frozen=True)
class Occurrence:
    """某条 blocker 的一次出现。"""
    blocker_id: str
    participant: str
    version_id: str
    ratio: float = 1.0      # 1.0 = 原文复现；<1 = 相似度提示
    declared: bool = False  # True = 评审自己声明「重提 Bn」，而非代码推断


@dataclass
class LedgerEntry:
    """台账中的一条分歧及其跨版本生命周期。"""
    id: str
    severity: str
    text: str
    raised_by: str
    raised_at: str          # 首次出现的版本号
    occurrences: list[Occurrence]
    disposition: str | None = None   # 主编在下一版变更清单中声明的处置
    reason: str = ""
    unanswered: bool = False         # 下一版用了编号格式却漏掉了这条
    recurs_of: str | None = None     # 评审声明「本条是 B<n> 的重提」
    recurrences: list[Occurrence] = None  # 声称处理后仍疑似重提

    def __post_init__(self):
        if self.recurrences is None:
            self.recurrences = []


@dataclass(frozen=True)
class VersionStat:
    version_id: str
    author: str
    counts: dict[str, int]
    votes: dict[str, str]
    stalled: bool  # 相对上一版，硬伤数未下降


_DISPOSITION_RE = re.compile(
    r"^\s*[-*]?\s*(B\d+)\s*[:：]?\s*(采纳|部分采纳|拒绝)\s*[:：,，。；;—\-]*\s*(.*)$"
)
_DECLARED_RE = re.compile(r"[（(]\s*重提\s*(B\d+)\s*[)）]")


def parse_dispositions(changelog: str) -> dict[str, tuple[str, str]]:
    """从变更清单里解析「B<n>: 采纳/部分采纳/拒绝 + 理由」；无编号格式则返回空字典。"""
    out: dict[str, tuple[str, str]] = {}
    for line in changelog.splitlines():
        m = _DISPOSITION_RE.match(line)
        if m:
            out[m.group(1)] = (m.group(2), m.group(3).strip())
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _split_declaration(text: str) -> tuple[str, str | None]:
    """把「（重提 B1）」从正文里剥离成结构化字段。"""
    m = _DECLARED_RE.search(text)
    if not m:
        return text.strip(), None
    return _DECLARED_RE.sub("", text, count=1).strip(" 　:：,，-—"), m.group(1)


def _blockers_on(disc: Discussion, version_id: str) -> list[tuple[str, Blocker]]:
    """某版本上的全部 BLOCK 条目，按名册顺序、票内行序。"""
    order = {p: i for i, p in enumerate(disc.participants)}
    votes = [v for v in disc.vote_log
             if v.version_id == version_id and v.verdict is Verdict.BLOCK]
    votes.sort(key=lambda v: order.get(v.participant, len(order)))
    return [(v.participant, b) for v in votes for b in v.blockers]


def blocker_ledger(disc: Discussion) -> list[LedgerEntry]:
    """把 vote_log 横向整理成分歧台账：编号、处置核对、疑似重提。"""
    entries: list[LedgerEntry] = []
    by_norm: dict[str, LedgerEntry] = {}
    for draft in disc.drafts:
        for participant, blk in _blockers_on(disc, draft.version_id):
            text, declared_of = _split_declaration(blk.text)
            key = _norm(text)
            entry = by_norm.get(key)
            if entry is None:
                entry = LedgerEntry(
                    id=f"B{len(entries) + 1}", severity=blk.severity, text=text,
                    raised_by=participant, raised_at=draft.version_id, occurrences=[],
                    recurs_of=declared_of,
                )
                entries.append(entry)
                by_norm[key] = entry
            entry.occurrences.append(Occurrence(entry.id, participant, draft.version_id))

    # 主编在「下一版」变更清单中对各 blocker 的处置；无编号格式的版本整版跳过核对
    for prev, nxt in zip(disc.drafts, disc.drafts[1:]):
        dispositions = parse_dispositions(nxt.changelog)
        if not dispositions:
            continue
        for entry in entries:
            if not any(o.version_id == prev.version_id for o in entry.occurrences):
                continue
            if entry.id in dispositions:
                if entry.disposition is None:
                    entry.disposition, entry.reason = dispositions[entry.id]
            else:
                entry.unanswered = True

    # 声称「采纳/部分采纳」之后，后续版本中的重提：以评审显式声明为准，相似度仅作补充提示
    index = {e.id: e for e in entries}
    for entry in entries:
        if entry.disposition not in ("采纳", "部分采纳"):
            continue
        last_seen = max(d.number for d in disc.drafts
                        if any(o.version_id == d.version_id for o in entry.occurrences))
        for later in (d for d in disc.drafts if d.number > last_seen):
            for participant, blk in _blockers_on(disc, later.version_id):
                text, declared_of = _split_declaration(blk.text)
                other_id = _id_of(entries, text)
                if other_id == entry.id:
                    continue
                if declared_of == entry.id:
                    entry.recurrences.append(
                        Occurrence(other_id, participant, later.version_id, 1.0, True))
                    continue
                if declared_of:   # 声明了但指向别人，不再用相似度二次猜测
                    continue
                ratio = difflib.SequenceMatcher(None, _norm(entry.text), _norm(text)).ratio()
                if ratio >= RECURRENCE_RATIO:
                    entry.recurrences.append(
                        Occurrence(other_id, participant, later.version_id, round(ratio, 2)))
    return entries


def _id_of(entries: list[LedgerEntry], text: str) -> str:
    key = _norm(text)
    for e in entries:
        if _norm(e.text) == key:
            return e.id
    raise KeyError(text)


def version_stats(disc: Discussion) -> list[VersionStat]:
    """每版的 blocker 分级计数与表态；stalled = 硬伤数相对上一版未下降。"""
    stats: list[VersionStat] = []
    prev_hard: int | None = None
    for draft in disc.drafts:
        blockers = _blockers_on(disc, draft.version_id)
        counts = {s: sum(1 for _, b in blockers if b.severity == s) for s in SEVERITIES}
        votes = {v.participant: v.verdict.value for v in disc.vote_log
                 if v.version_id == draft.version_id}
        hard = counts["硬伤"]
        stats.append(VersionStat(draft.version_id, draft.author, counts, votes,
                                 stalled=prev_hard is not None and hard >= prev_hard and hard > 0))
        prev_hard = hard
    return stats


@dataclass(frozen=True)
class StallVerdict:
    """讨论是否已进入打转。仅陈述机械事实，判断是否继续由调用方决定。"""
    stalled: bool
    reason: str = ""


def stall_check(disc: Discussion) -> StallVerdict:
    """连续两版硬伤数未下降、且期间出现过重提 → 判定为打转。

    两个条件缺一不可：硬伤持平但每轮都是新问题，说明讨论在深挖而非原地打转。
    """
    stats = version_stats(disc)
    if len(stats) < 3:
        return StallVerdict(False)
    if not (stats[-1].stalled and stats[-2].stalled):
        return StallVerdict(False)
    recent = {stats[-1].version_id, stats[-2].version_id}
    recurrences = [r for e in blocker_ledger(disc) for r in e.recurrences
                   if r.version_id in recent]
    if not recurrences:
        return StallVerdict(False)
    who = "、".join(dict.fromkeys(r.participant for r in recurrences))
    return StallVerdict(
        True,
        f"硬伤数连续两版未下降，且期间出现 {len(recurrences)} 处重提（{who}）——"
        f"同一问题被反复提出，继续论证不太可能收敛。",
    )


def render_diff(prev: Draft, cur: Draft, max_lines: int = 120) -> str:
    """两版草案之间的 unified diff，供评审核对主编声称的处置是否属实。"""
    lines = list(difflib.unified_diff(
        prev.text.splitlines(), cur.text.splitlines(),
        fromfile=prev.version_id, tofile=cur.version_id, lineterm="", n=2))
    if not lines:
        return f"（{prev.version_id} → {cur.version_id}：正文未改动）"
    if len(lines) > max_lines:
        omitted = len(lines) - max_lines
        lines = lines[:max_lines] + [f"…（diff 过长，已截断 {omitted} 行，完整新版见上方草案全文）"]
    return "\n".join(lines)


def sanitize_slug(topic: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"[^\w-]+", "-", topic).strip("-_")
    return cleaned[:max_len].strip("-_") or "untitled"


def divergence_signals(disc: Discussion) -> dict[str, int]:
    """三类打转信号的计数（均为机械可判定的事实，不含价值判断）。"""
    ledger = blocker_ledger(disc)
    return {
        "重提": sum(len(e.recurrences) for e in ledger),
        "未回应": sum(1 for e in ledger if e.unanswered),
        "硬伤数不降": sum(1 for s in version_stats(disc) if s.stalled),
    }


def render_divergence(disc: Discussion) -> str:
    """分歧演化章节：版本趋势表 + blocker 台账。从未有人 BLOCK 过则返回空串。"""
    ledger = blocker_ledger(disc)
    if not ledger:
        return ""
    stats = version_stats(disc)
    lines = ["## 分歧演化", "",
             "| 版本 | 主编 | 硬伤 | 偏好 | 待验证 | 表态 |",
             "|---|---|---|---|---|---|"]
    for s in stats:
        votes = " ".join(f"{p}:{v}" for p, v in s.votes.items()) or "—"
        mark = " ⚠" if s.stalled else ""
        lines.append(f"| {s.version_id}{mark} | {s.author} | {s.counts['硬伤']} | "
                     f"{s.counts['偏好']} | {s.counts['待验证']} | {votes} |")
    lines += ["", "> ⚠ 标记的版本：硬伤数相对上一版未下降。", "", "### 分歧台账", ""]
    for e in ledger:
        seen = "、".join(dict.fromkeys(o.version_id for o in e.occurrences))
        lines.append(f"**{e.id}** [{e.severity}] {e.text}")
        lines.append(f"- 首次提出：{e.raised_by} @ {e.raised_at}；出现于 {seen}")
        if e.recurs_of:
            lines.append(f"- ↩ 评审声明这是 {e.recurs_of} 的重提")
        if e.disposition:
            reason = f"——{e.reason}" if e.reason else ""
            lines.append(f"- 主编处置：{e.disposition}{reason}")
        if e.unanswered:
            lines.append("- ⚠ 下一版变更清单使用了编号格式，但未回应本条")
        for r in e.recurrences:
            how = "评审声明重提" if r.declared else f"疑似重提（文本相似 {r.ratio}，仅作提示）"
            lines.append(f"- ⚠ {how}：{r.participant} @ {r.version_id}（{r.blocker_id}）")
        lines.append("")
    return "\n".join(lines)

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
                        draft: Draft, transcript: str, first_cycle: bool,
                        ledger: list[LedgerEntry] | None = None,
                        diff: str | None = None) -> str:
    extra = ("\n本周期是第 1 评审周期：若你选择 ACCEPT，必须逐条回应变更清单中列出的每一个分歧点。"
             if first_cycle else "")
    diff_block = ""
    if diff:
        diff_block = f"""

本版相对上一版的实际改动（unified diff）：
```
{diff}
```
请**核对**变更清单里声称的处置与上面的实际改动是否一致：
声称"采纳"却没有对应改动、或悄悄回退了此前已解决的内容，都属于硬伤，应据实 BLOCK。"""
    open_items = ""
    if ledger:
        listing = "\n".join(f"- {e.id} [{e.severity}] {e.text}"
                            f"（{e.raised_by} 提出"
                            f"{'，主编声称' + e.disposition if e.disposition else ''}）"
                            for e in ledger)
        open_items = f"""

此前讨论中提出过的分歧（编号台账）：
{listing}

若你本次的某条 BLOCK 是在重提上表中**仍未解决**的问题，请在该条描述前标注编号，
写成：`- [硬伤] （重提 B1）你的描述`。这能让记录如实反映讨论是否在原地打转。"""
    return f"""{_preamble(name, lens, "你是当前版本的评审者。同周期其他评审者的发言对你不可见。", topic, constraints)}

已公开的讨论记录：
{transcript}

当前草案 {draft.version_id}（作者：{draft.author}）：
{draft.text}

上一版变更清单：
{draft.changelog}{diff_block}{open_items}

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
                          transcript: str, ledger: list[LedgerEntry] | None = None) -> str:
    ids = {_norm(e.text): e.id for e in ledger} if ledger else {}
    if blockers:
        blk = "\n".join(
            (f"- {ids[_norm(_split_declaration(b.text)[0])]} （{p}）[{b.severity}] {b.text}"
             if _norm(_split_declaration(b.text)[0]) in ids
             else f"- （{p}）[{b.severity}] {b.text}")
            for p, b in blockers)
    else:
        blk = "（本轮无 BLOCK，修订由新的人类约束触发）"
    numbering = """

变更清单必须使用上面的编号，每条以编号开头，例如：
B1: 采纳，改用实测阈值
B2: 部分采纳，只保留必要项
B3: 拒绝，超出本议题范围
（评审会机械核对你是否逐条处理，请勿遗漏任何一条。）""" if ids and blockers else ""
    return f"""{_preamble(name, lens, "你是本周期的轮值主编。", topic, constraints)}

已公开的讨论记录：
{transcript}

当前草案 {draft.version_id}（作者：{draft.author}）：
{draft.text}

待处理的全部 blocker：
{blk}

请产出下一版草案。变更清单必须逐条说明对每个 blocker（及每条新的人类约束）的处理：
采纳 / 部分采纳 / 拒绝并说明理由。{numbering}
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


def render_final(disc: Discussion, recommendation: str | None,
                 stall: StallVerdict | None = None) -> str:
    result = disc.outcome()
    lines = [
        f"# 圆桌结果：{result.value}", "",
        f"**议题：** {disc.topic}", "",
        f"**参与者：** {', '.join(disc.participants)} · 评审周期 {disc.cycle}/{disc.max_rounds}", "",
    ]
    if stall is not None and stall.stalled:
        lines += [
            "> **提前散会**：" + stall.reason,
            "> 建议转为实验或人工裁决——见下方「共同盲区与外部验证建议」与「分歧演化」。", "",
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
    divergence = render_divergence(disc)
    if divergence:
        lines += ["", divergence]
    lines += ["", "## 共同盲区与外部验证建议", ""]
    if pending:
        lines += [f"- （{p}）{b.text}" for p, b in pending]
    else:
        lines.append("- 讨论中未显式标记待验证事实。")
    lines += ["", "> 三个模型可能共享同一种错误知识；关键决策请在模型之外验证。", ""]
    if recommendation:
        lines += ["## 附录：主编个人建议（未经全员认可，不构成共识）", "", recommendation, ""]
    return "\n".join(lines)
