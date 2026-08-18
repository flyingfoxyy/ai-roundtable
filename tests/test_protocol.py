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


if __name__ == "__main__":
    unittest.main()
