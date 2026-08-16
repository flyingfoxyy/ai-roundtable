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
