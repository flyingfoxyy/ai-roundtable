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
