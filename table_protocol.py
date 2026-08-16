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
