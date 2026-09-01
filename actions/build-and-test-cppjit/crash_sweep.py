#!/usr/bin/env python3
"""Runs cppjit's crash-class xfails one process each and reports each outcome.

A test marked `xfail(run=False)` is one that takes the interpreter down with
it, so pytest never reaches its own report. Running the class in one session
would spend the restart budget rebooting for whichever dies first, and the
survivors would then run against state the real suite never has.

The sweep reports, it does not gate. A crash denies the tests after it the
state they would have had, so "passes in isolation" is a lead to follow, not
a verdict that the marker is wrong. Only a collection that cannot run at all
fails the step.

Usage: crash_sweep.py [--timeout SECONDS]
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

PYTEST = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
          "--run-crashing-xfails"]

# pytest prints this once it has reached its own reporting; a killed
# interpreter takes it away, and the exit code does not (LLVM's signal
# handler leaves a plain status).
SUMMARY_WORDS = ("passed", "failed", "xfailed", "xpassed", "skipped",
                 "error", "no tests ran")

STALE = "passes in isolation -- marker may be stale"


def classify(output: str, timed_out: bool) -> str:
    """The verdict for one test, from its own output alone."""
    if "XPASS(strict)" in output:
        return STALE
    if timed_out:
        return "hangs"
    summary = [ln for ln in output.splitlines() if ln.startswith("=")
               and any(w in ln for w in SUMMARY_WORDS)]
    if not summary:
        return "still crashes (no report)"
    if any("no tests ran" in ln or "skipped" in ln for ln in summary):
        return "not run here (skipped)"
    return "still fails"


def collect() -> list[str]:
    """Node ids of the crash markers live on this platform, relative to cwd.

    Collection is rooted at the repository, the step runs in test/.
    """
    proc = subprocess.run(PYTEST + ["-q", "--collect-only"],
                          capture_output=True, text=True)
    if proc.returncode >= 2:
        raise RuntimeError(f"collection failed (rc={proc.returncode})\n"
                           f"{proc.stdout}{proc.stderr}")
    return [ln.removeprefix("test/") for ln in proc.stdout.splitlines()
            if "::" in ln]


def run_one(node_id: str, timeout: int) -> str:
    try:
        proc = subprocess.run(PYTEST + ["-rA", "--tb=line", node_id],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        return classify(expired.output.decode(errors="replace")
                        if expired.output else "", timed_out=True)
    return classify(proc.stdout + proc.stderr, timed_out=False)


def report(verdicts: list[tuple[str, str]]) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    body = "\n".join(f"{verdict}: {node_id}" for node_id, verdict in verdicts)
    with open(summary, "a") as handle:
        handle.write("### xfail crash sweep\n```\n"
                     f"{body or 'no crash-class markers on this platform'}\n"
                     "```\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    if "run-crashing-xfails" not in Path("conftest.py").read_text():
        print("::notice::conftest lacks --run-crashing-xfails; sweep skipped")
        return 0

    try:
        node_ids = collect()
    except RuntimeError as err:
        print(f"::error::crash-class {err}")
        return 1

    verdicts = []
    for node_id in node_ids:
        verdict = run_one(node_id, args.timeout)
        print(f"{verdict}: {node_id}", flush=True)
        verdicts.append((node_id, verdict))

    report(verdicts)
    if any(verdict == STALE for _, verdict in verdicts):
        print("::warning::crash-class markers passed in isolation; "
              "see the job summary")
    return 0


if __name__ == "__main__":
    sys.exit(main())
