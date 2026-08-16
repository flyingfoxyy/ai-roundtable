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
    def test_dir_naming_and_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            s1 = table.RunStore(base, "议题/名？")
            s2 = table.RunStore(base, "议题/名？")
            self.assertTrue(s1.sandbox.is_dir())
            self.assertNotEqual(s1.dir, s2.dir)
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
            self.assertEqual(text.count("[阶段0] A"), 2)  # proposal + draft 各一次，无重复
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


if __name__ == "__main__":
    unittest.main()
