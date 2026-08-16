#!/usr/bin/env python3
"""剧本驱动的假 CLI。用法: fake_cli.py <scenario_dir> <role>

剧本: <scenario_dir>/<role>/ 下的 001.txt, 002.txt …（按调用次序消费，.counter 计数）。
行为: 读 stdin 作为 prompt，存档到 <scenario_dir>/calls/<role>-<n>-prompt.txt，打印响应文件内容。
响应文件首行特殊指令:
  SLEEP <secs>   → 写 <role>-<n>.pid 后挂起（超时测试）
  BARRIER <path> → touch <path>、写 pid 后挂起 600s（kill 持久化测试）
  BIGERR         → 向 stderr 写 1MB，再把其余行写到 stdout
"""
import os
import pathlib
import sys
import time


def main() -> None:
    scenario = pathlib.Path(sys.argv[1])
    role = sys.argv[2]
    prompt = sys.stdin.read()
    d = scenario / role
    counter = d / ".counter"
    n = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(n))
    calls = scenario / "calls"
    calls.mkdir(exist_ok=True)
    (calls / f"{role}-{n}-prompt.txt").write_text(prompt, encoding="utf-8")
    resp_file = d / f"{n:03d}.txt"
    if not resp_file.exists():
        print(f"fake_cli: {role} 没有第 {n} 号响应", file=sys.stderr)
        sys.exit(3)
    resp = resp_file.read_text(encoding="utf-8")
    first = resp.splitlines()[0] if resp else ""
    if first.startswith("SLEEP "):
        (calls / f"{role}-{n}.pid").write_text(str(os.getpid()))
        time.sleep(float(first.split()[1]))
        sys.exit(0)
    if first.startswith("BARRIER "):
        pathlib.Path(first.split(maxsplit=1)[1]).touch()
        (calls / f"{role}-{n}.pid").write_text(str(os.getpid()))
        time.sleep(600)
        sys.exit(0)
    if first == "BIGERR":
        sys.stderr.write("x" * 1_000_000)
        sys.stdout.write(resp.partition("\n")[2])
        sys.exit(0)
    sys.stdout.write(resp)


if __name__ == "__main__":
    main()
