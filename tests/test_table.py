import contextlib
import os
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


class TestConfig(unittest.TestCase):
    def test_falls_back_to_bak_template_when_no_user_config(self):
        """无自定义 table.toml 时使用随代码分发的 table.toml.bak，且不创建任何文件。"""
        with tempfile.TemporaryDirectory() as td, contextlib.chdir(td):
            pcs = table.load_config(None)
            self.assertFalse(pathlib.Path("table.toml").exists())   # 不自动生成
        self.assertEqual([p.name for p in pcs], ["Claude", "Gemini", "Codex"])
        self.assertEqual(pcs[0].cmd, ("claude", "-p"))
        self.assertEqual(pcs[0].timeout, table.DEFAULT_TIMEOUT)

    def test_user_config_takes_precedence_over_bak(self):
        with tempfile.TemporaryDirectory() as td, contextlib.chdir(td):
            pathlib.Path("table.toml").write_text(
                '[[participants]]\nname = "Mine1"\ncmd = ["a"]\n'
                '[[participants]]\nname = "Mine2"\ncmd = ["b"]\n',
                encoding="utf-8",
            )
            pcs = table.load_config(None)
        self.assertEqual([p.name for p in pcs], ["Mine1", "Mine2"])

    def test_default_timeout_accommodates_slow_reasoning_models(self):
        """实测：Claude 单次调用常达 180~290 秒，300 秒会频繁误判超时。"""
        with tempfile.TemporaryDirectory() as td:
            f = pathlib.Path(td) / "t.toml"
            f.write_text('[[participants]]\nname = "X"\ncmd = ["a"]\n'
                         '[[participants]]\nname = "Y"\ncmd = ["b"]\n', encoding="utf-8")
            pcs = table.load_config(f)
        self.assertGreaterEqual(pcs[0].timeout, 600)

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
            '[[participants]]\nname = "X"\ncmd = ["a"]\n',
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


class TestRunStore(unittest.TestCase):
    def roster(self):
        return [table.ParticipantConfig("A", ("a",), "lensA", 7),
                table.ParticipantConfig("B", ("b", "-p"))]

    def test_dir_naming_and_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            s1 = table.RunStore.create(base, "议题/名？", self.roster())
            s2 = table.RunStore.create(base, "议题/名？", self.roster())
            self.assertTrue(s1.sandbox.is_dir())
            self.assertNotEqual(s1.dir, s2.dir)
            self.assertIn("议题-名", s1.dir.name)

    def test_incremental_transcript_and_atomic_state(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            store = table.RunStore.create(base, "题", self.roster())
            disc = tp.Discussion("题", ["A", "B"], 5)
            disc.add_proposal("A", "分析", "方案")
            store.transcript(disc)
            store.state(disc)
            disc.add_draft("A", "v1", "log")
            store.transcript(disc)
            text = (store.dir / "transcript.md").read_text(encoding="utf-8")
            self.assertEqual(text.count("[阶段0] A"), 2)  # proposal + draft 各一次，无重复
            snap = json.loads((store.dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(snap["topic"], "题")

    def test_state_persists_roster_for_resume(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            store = table.RunStore.create(pathlib.Path(td), "题", self.roster())
            store.state(tp.Discussion("题", ["A", "B"], 5))
            snap = json.loads((store.dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(table.roster_from_json(snap["roster"]), self.roster())

    def test_reopen_continues_transcript_without_duplicating(self):
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            store = table.RunStore.create(base, "题", self.roster())
            disc = tp.Discussion("题", ["A", "B"], 5)
            disc.add_proposal("A", "分析", "首场发言")
            store.transcript(disc)
            reopened = table.RunStore.reopen(store.dir, self.roster(), len(disc.events))
            disc.add_note("Human", "续会发言")
            reopened.transcript(disc)
            text = (store.dir / "transcript.md").read_text(encoding="utf-8")
            self.assertEqual(text.count("首场发言"), 1)   # 旧事件不重写
            self.assertIn("续会发言", text)
            self.assertEqual(reopened.sandbox, store.sandbox)

    def test_final_archives_previous_versions(self):
        with tempfile.TemporaryDirectory() as td:
            store = table.RunStore.create(pathlib.Path(td), "题", self.roster())
            store.final("第一次结论")
            store.final("第二次结论")
            store.final("第三次结论")
            self.assertEqual((store.dir / "final-1.md").read_text(encoding="utf-8"), "第一次结论")
            self.assertEqual((store.dir / "final-2.md").read_text(encoding="utf-8"), "第二次结论")
            self.assertEqual((store.dir / "final.md").read_text(encoding="utf-8"), "第三次结论")

    def test_audit_raw_final(self):
        with tempfile.TemporaryDirectory() as td:
            store = table.RunStore.create(pathlib.Path(td), "题", self.roster())
            store.audit({"a": 1})
            store.audit({"b": "中文"})
            store.raw("X", "坏输出")
            store.final("终局")
            lines = (store.dir / "session.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("中文", lines[1])
            self.assertEqual(len(list((store.dir / "raw").iterdir())), 1)
            self.assertEqual((store.dir / "final.md").read_text(encoding="utf-8"), "终局")


if __name__ == "__main__":
    unittest.main()


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


class TestCliArgs(unittest.TestCase):
    """main() 的参数校验（不触发真实会议）：断言具体报错，避免被 argparse 的通用错误蒙混过关。"""

    def assert_exits_with(self, argv, needle):
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                table.main(argv)
        message = f"{cm.exception} {err.getvalue()}"
        self.assertIn(needle, message)

    def test_topic_required_without_continue(self):
        self.assert_exits_with([], "议题")

    def test_info_file_and_inline_info_are_exclusive(self):
        self.assert_exits_with(["--continue", "内联信息", "--info-file", "x.md"], "二选一")

    def test_from_requires_continue(self):
        self.assert_exits_with(["议题", "--from", "runs/x"], "--from")

    def test_max_rounds_must_be_positive(self):
        self.assert_exits_with(["议题", "--max-rounds", "0"], "--max-rounds")

    def test_missing_info_file_reports_clearly(self):
        self.assert_exits_with(["--continue", "--info-file", "/nonexistent/bench.md"],
                               "/nonexistent/bench.md")

    def test_continue_without_any_resumable_run(self):
        with tempfile.TemporaryDirectory() as td:
            self.assert_exits_with(
                ["--continue", "--runs-dir", str(pathlib.Path(td) / "runs")], "可续")


if __name__ == "__main__":
    unittest.main()
