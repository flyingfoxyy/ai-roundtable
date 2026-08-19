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


class TestSnapshotRestore(unittest.TestCase):
    def build(self):
        disc = tp.Discussion("续会议题", ["A", "B", "C"], 5)
        disc.add_proposal("A", "分析A", "方案A")
        disc.add_proposal("B", "分析B", "方案B")
        disc.add_draft("A", "v1文", "分歧点", "主编说明")
        disc.record_vote("B", tp.ParsedVerdict(tp.Verdict.ACCEPT, "残余风险X"), "论述B")
        disc.record_vote("C", tp.ParsedVerdict(
            tp.Verdict.BLOCK, "raw", (tp.Blocker("硬伤", "缺X"), tp.Blocker("待验证", "QPS"))), "论述C")
        disc.add_draft("B", "v2文", "变更清单")
        disc.record_invalid("C", "两次解析失败")
        disc.add_constraint("必须支持离线")
        disc.cycle = 2
        return disc

    def test_roundtrip_preserves_everything(self):
        import json
        disc = self.build()
        back = tp.restore(json.loads(json.dumps(tp.snapshot(disc))))
        self.assertEqual(back.topic, disc.topic)
        self.assertEqual(back.participants, disc.participants)
        self.assertEqual(back.max_rounds, disc.max_rounds)
        self.assertEqual(back.cycle, disc.cycle)
        self.assertEqual(back.constraints, disc.constraints)
        self.assertEqual(back.proposals, disc.proposals)
        self.assertEqual(back.drafts, disc.drafts)
        self.assertEqual(back.events, disc.events)
        self.assertEqual(back.votes, disc.votes)
        self.assertEqual(back.vote_log, disc.vote_log)
        self.assertEqual(back.unaddressed_constraints, disc.unaddressed_constraints)
        self.assertIs(back.outcome(), disc.outcome())

    def test_roundtrip_preserves_vote_binding_and_blockers(self):
        import json
        disc = tp.Discussion("题", ["A", "B"], 5)
        disc.add_draft("A", "v1文", "log")
        disc.record_vote("B", tp.ParsedVerdict(
            tp.Verdict.BLOCK, "raw", (tp.Blocker("硬伤", "缺X"),)), "s")
        back = tp.restore(json.loads(json.dumps(tp.snapshot(disc))))
        self.assertEqual(back.votes["B"].version_id, back.current.version_id)  # 票仍绑定该版本
        self.assertEqual(back.active_blockers(), [("B", tp.Blocker("硬伤", "缺X"))])
        back.add_draft("B", "v2文", "改了")
        self.assertEqual(back.votes, {})        # 复原后的状态机行为不变：新版本清票

    def test_restore_rejects_foreign_or_old_format(self):
        with self.assertRaises(ValueError):
            tp.restore({"topic": "旧", "participants": ["A", "B"], "max_rounds": 5})  # 无 format
        with self.assertRaises(ValueError):
            tp.restore({"format": 999, "topic": "未来", "participants": ["A", "B"]})


class TestBlockerLedger(unittest.TestCase):
    """blocker 台账：跨版本追踪每条分歧的生命周期。"""

    def blk(self, *items):
        return tp.ParsedVerdict(tp.Verdict.BLOCK, "raw",
                                tuple(tp.Blocker(s, t) for s, t in items))

    def test_ids_assigned_deterministically_by_first_appearance(self):
        disc = tp.Discussion("题", ["A", "B", "C"], 5)
        disc.add_draft("A", "v1", "log")
        disc.record_vote("C", self.blk(("硬伤", "问题Z")), "s")
        disc.record_vote("B", self.blk(("硬伤", "问题Y"), ("偏好", "问题X")), "s")
        ledger = tp.blocker_ledger(disc)
        # 同一版本内按名册顺序（B 先于 C），票内按行序
        self.assertEqual([(e.id, e.text) for e in ledger],
                         [("B1", "问题Y"), ("B2", "问题X"), ("B3", "问题Z")])
        self.assertEqual(ledger[0].raised_by, "B")
        self.assertEqual(ledger[0].raised_at, disc.current.version_id)
        self.assertEqual(tp.blocker_ledger(disc), ledger)      # 可复现

    def test_identical_text_across_versions_reuses_id(self):
        disc = tp.Discussion("题", ["A", "B"], 5)
        disc.add_draft("A", "v1", "log")
        disc.record_vote("B", self.blk(("硬伤", "同一句话")), "s")
        disc.add_draft("A", "v2", " B1: 采纳")
        disc.record_vote("B", self.blk(("硬伤", " 同一句话 ")), "s")   # 归一化后相同
        ledger = tp.blocker_ledger(disc)
        self.assertEqual([e.id for e in ledger], ["B1"])
        self.assertEqual(len(ledger[0].occurrences), 2)

    def test_parse_dispositions_lenient_and_degrades(self):
        text = "- B1: 采纳，已改用实测阈值\n* B2 部分采纳 —— 只保留必要项\nB3：拒绝，超出议题范围"
        self.assertEqual(tp.parse_dispositions(text),
                         {"B1": ("采纳", "已改用实测阈值"),
                          "B2": ("部分采纳", "只保留必要项"),
                          "B3": ("拒绝", "超出议题范围")})
        self.assertEqual(tp.parse_dispositions("自由文本的变更清单，没有编号"), {})

    def test_flags_unanswered_blocker(self):
        disc = tp.Discussion("题", ["A", "B"], 5)
        disc.add_draft("A", "v1", "log")
        disc.record_vote("B", self.blk(("硬伤", "问题P"), ("硬伤", "问题Q")), "s")
        disc.add_draft("A", "v2", "B1: 采纳，已修")          # 只回应了 B1
        ledger = {e.id: e for e in tp.blocker_ledger(disc)}
        self.assertEqual(ledger["B1"].disposition, "采纳")
        self.assertIsNone(ledger["B2"].disposition)
        self.assertTrue(ledger["B2"].unanswered)
        self.assertFalse(ledger["B1"].unanswered)

    def test_no_numbering_in_changelog_skips_verification(self):
        """老会议记录的变更清单没有编号 → 整版跳过核对，不产生满屏 ⚠。"""
        disc = tp.Discussion("题", ["A", "B"], 5)
        disc.add_draft("A", "v1", "log")
        disc.record_vote("B", self.blk(("硬伤", "问题P"), ("硬伤", "问题Q")), "s")
        disc.add_draft("A", "v2", "自由文本：我修了一些东西")
        ledger = tp.blocker_ledger(disc)
        self.assertFalse(any(e.unanswered for e in ledger))

    def test_declaration_marker_extracted_out_of_text(self):
        """「（重提 B1）」是结构化信息，不该留在正文里污染台账与相似度比较。"""
        disc = tp.Discussion("题", ["A", "B", "C"], 5)
        disc.add_draft("A", "v1", "log")
        disc.record_vote("B", self.blk(("硬伤", "阈值缺出处")), "s")
        disc.add_draft("A", "v2", "B1: 采纳")
        disc.record_vote("C", self.blk(("硬伤", "（重提 B1）换个说法说同一件事")), "s")
        ledger = {e.id: e for e in tp.blocker_ledger(disc)}
        self.assertEqual(ledger["B2"].text, "换个说法说同一件事")   # 标记已剥离
        self.assertEqual(ledger["B2"].recurs_of, "B1")             # 提升为字段
        self.assertIsNone(ledger["B1"].recurs_of)
        self.assertIn("这是 B1 的重提", tp.render_divergence(disc))

    def test_detects_declared_recurrence(self):
        """评审显式声明「重提 B1」→ 确定的重提记录，不依赖文本相似度。"""
        disc = tp.Discussion("题", ["A", "B", "C"], 5)
        disc.add_draft("A", "v1", "log")
        disc.record_vote("B", self.blk(("硬伤", "阈值没有基准测试支撑")), "s")
        disc.add_draft("A", "v2", "B1: 采纳，已补充说明")
        disc.record_vote("C", self.blk(("硬伤", "（重提 B1）换个说法：这个数字依然没有出处")), "s")
        ledger = {e.id: e for e in tp.blocker_ledger(disc)}
        rec = ledger["B1"].recurrences
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0].participant, "C")
        self.assertTrue(rec[0].declared)          # 声明的，而非猜的

    def test_detects_suspected_recurrence_by_similarity(self):
        disc = tp.Discussion("题", ["A", "B", "C"], 5)
        disc.add_draft("A", "v1", "log")
        disc.record_vote("B", self.blk(("硬伤", "阈值没有基准测试支撑")), "s")
        disc.add_draft("A", "v2", "B1: 采纳，已补充说明")
        disc.record_vote("C", self.blk(("硬伤", "阈值没有实测数据支撑")), "s")  # 换个说法重提
        ledger = {e.id: e for e in tp.blocker_ledger(disc)}
        rec = ledger["B1"].recurrences
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0].blocker_id, "B2")
        self.assertEqual(rec[0].participant, "C")
        self.assertFalse(rec[0].declared)         # 仅为相似度提示
        self.assertGreaterEqual(rec[0].ratio, tp.RECURRENCE_RATIO)

    def test_no_recurrence_when_texts_unrelated(self):
        disc = tp.Discussion("题", ["A", "B", "C"], 5)
        disc.add_draft("A", "v1", "log")
        disc.record_vote("B", self.blk(("硬伤", "缺少并发写入的实测数据")), "s")
        disc.add_draft("A", "v2", "B1: 采纳")
        # 同一主题下的不同问题（实测标定相似度 0.36），不得误报为重提
        disc.record_vote("C", self.blk(("硬伤", "缺少数据迁移的回滚方案")), "s")
        ledger = {e.id: e for e in tp.blocker_ledger(disc)}
        self.assertEqual(ledger["B1"].recurrences, [])

    def test_version_stats_and_stalled_severity_flag(self):
        disc = tp.Discussion("题", ["A", "B"], 5)
        disc.add_draft("A", "v1", "log")
        disc.record_vote("B", self.blk(("硬伤", "P1"), ("偏好", "P2")), "s")
        disc.add_draft("A", "v2", "B1: 采纳")
        disc.record_vote("B", self.blk(("硬伤", "P3")), "s")
        disc.add_draft("A", "v3", "B3: 采纳")
        disc.record_vote("B", self.blk(("硬伤", "P4")), "s")
        stats = tp.version_stats(disc)
        self.assertEqual([(s.version_id[:2], s.counts["硬伤"], s.counts["偏好"]) for s in stats],
                         [("v1", 1, 1), ("v2", 1, 0), ("v3", 1, 0)])
        self.assertEqual(stats[0].votes, {"B": "BLOCK"})
        self.assertFalse(stats[0].stalled)      # 首版无从比较
        self.assertTrue(stats[1].stalled)       # 硬伤数未下降
        self.assertTrue(stats[2].stalled)

    def test_empty_discussion_is_safe(self):
        disc = tp.Discussion("题", ["A", "B"], 5)
        self.assertEqual(tp.blocker_ledger(disc), [])
        self.assertEqual(tp.version_stats(disc), [])


class TestRenderDivergence(unittest.TestCase):
    def build(self):
        disc = tp.Discussion("题", ["A", "B", "C"], 5)
        disc.add_draft("A", "v1", "分歧点")
        disc.record_vote("B", tp.ParsedVerdict(tp.Verdict.BLOCK, "raw", (
            tp.Blocker("硬伤", "阈值没有基准测试支撑"),
            tp.Blocker("偏好", "命名不一致"))), "s")
        disc.record_vote("C", tp.ParsedVerdict(tp.Verdict.ACCEPT, "可容忍"), "s")
        disc.add_draft("B", "v2", "B1: 采纳，已补实测\nB2: 拒绝，超出范围")
        disc.record_vote("C", tp.ParsedVerdict(tp.Verdict.BLOCK, "raw", (
            tp.Blocker("硬伤", "（重提 B1）数字依然没有出处"),)), "s")
        return disc

    def test_renders_table_ledger_and_signals(self):
        out = tp.render_divergence(self.build())
        self.assertIn("## 分歧演化", out)
        self.assertIn("B1", out)
        self.assertIn("阈值没有基准测试支撑", out)
        self.assertIn("采纳", out)
        self.assertIn("重提", out)
        self.assertIn("C", out)
        self.assertIn("硬伤", out)

    def test_signal_summary_counts_warnings(self):
        signals = tp.divergence_signals(self.build())
        self.assertEqual(signals["重提"], 1)
        self.assertEqual(signals["硬伤数不降"], 1)   # v2 与 v1 同为 1 条硬伤
        self.assertEqual(signals["未回应"], 0)
        self.assertEqual(tp.divergence_signals(tp.Discussion("题", ["A", "B"], 5)),
                         {"重提": 0, "未回应": 0, "硬伤数不降": 0})

    def test_final_document_embeds_divergence_section(self):
        doc = tp.render_final(self.build(), None)
        self.assertIn("## 分歧演化", doc)
        self.assertIn("B1", doc)

    def test_divergence_omitted_when_never_blocked(self):
        disc = tp.Discussion("题", ["A", "B"], 5)
        disc.add_draft("A", "v1", "log")
        disc.record_vote("B", tp.ParsedVerdict(tp.Verdict.ACCEPT, "风险声明"), "s")
        self.assertEqual(tp.render_divergence(disc), "")
        self.assertNotIn("分歧演化", tp.render_final(disc, None))


class TestNumberedPrompts(unittest.TestCase):
    """编号进 prompt：主编按编号回应、评审可声明重提。"""

    def build(self):
        disc = tp.Discussion("题", ["A", "B", "C"], 5)
        disc.add_draft("A", "v1文", "分歧点")
        disc.record_vote("B", tp.ParsedVerdict(tp.Verdict.BLOCK, "raw", (
            tp.Blocker("硬伤", "缺实测数据"), tp.Blocker("偏好", "命名不一致"))), "s")
        return disc

    def test_revision_prompt_numbers_blockers_and_demands_per_id_reply(self):
        disc = self.build()
        p = tp.build_revision_prompt("题", [], "B", "", disc.current,
                                     disc.active_blockers(), "记录",
                                     ledger=tp.blocker_ledger(disc))
        self.assertIn("B1 （B）[硬伤] 缺实测数据", p)
        self.assertIn("B2 （B）[偏好] 命名不一致", p)
        self.assertIn("B1: 采纳", p)          # 格式示例
        self.assertIn("每条以编号开头", p)

    def test_revision_prompt_without_ledger_keeps_plain_list(self):
        disc = self.build()
        p = tp.build_revision_prompt("题", [], "B", "", disc.current,
                                     disc.active_blockers(), "记录")
        self.assertIn("（B）[硬伤] 缺实测数据", p)
        self.assertNotIn("每条以编号开头", p)

    def test_review_prompt_lists_open_blockers_for_recurrence_declaration(self):
        disc = self.build()
        p = tp.build_review_prompt("题", [], "C", "", disc.current, "记录", False,
                                   ledger=tp.blocker_ledger(disc))
        self.assertIn("B1 [硬伤] 缺实测数据", p)
        self.assertIn("（重提 B1）", p)        # 声明格式说明
        self.assertIn("未解决", p)

    def test_review_prompt_without_ledger_unchanged(self):
        disc = self.build()
        p = tp.build_review_prompt("题", [], "C", "", disc.current, "记录", False)
        self.assertNotIn("重提", p)


if __name__ == "__main__":
    unittest.main()
