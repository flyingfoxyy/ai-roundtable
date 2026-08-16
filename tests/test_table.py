import contextlib
import pathlib
import tempfile
import unittest

import table
import table_protocol as tp


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


if __name__ == "__main__":
    unittest.main()
