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
                try:
                    os.kill(int(pid_file.read_text()), sig.SIGKILL)
                except OSError:
                    pass
        transcript = self.read("transcript.md")
        self.assertIn("pa", transcript)
        self.assertIn("v1文", transcript)          # 崩溃前事件均已落盘
        state = json.loads(self.read("state.json"))
        self.assertEqual(len(state["drafts"]), 1)


if __name__ == "__main__":
    unittest.main()
