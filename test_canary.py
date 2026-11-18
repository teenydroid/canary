#!/usr/bin/env python3
"""Tests for canary: run with `python3 -m unittest -v` from this folder.

Every end-to-end test drives canary.py exactly like a user would: a
planted fault, a gate, an exit code. The fixtures hold the gates; two of
them are broken on purpose (blind_gate approves everything, loud_gate
says REJECTED and still exits 0).
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CANARY = os.path.join(HERE, "canary.py")
FIX = os.path.join(HERE, "fixtures")

PY = sys.executable or "python3"

# Toy rewriter used by the refine tests: reverses the text, so a fault
# written as "ODOT" becomes "TODO" after one pass.
REVERSE_CMD = [PY, "-c",
               "import sys; sys.stdout.write(sys.stdin.read()[::-1])"]
REVERSE_CMD_STR = " ".join(shlex.quote(x) for x in REVERSE_CMD)


def fixture(name):
    path = os.path.join(FIX, name)
    os.chmod(path, 0o755)
    return path


def run_canary(argv):
    return subprocess.run([PY, CANARY] + argv, cwd=HERE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=120)


class UnitTests(unittest.TestCase):
    def test_detect_verdict_whole_words(self):
        import canary
        self.assertEqual(canary.detect_verdict("REJECTED: TODO"), "rejected")
        self.assertEqual(canary.detect_verdict("gate failed"), "failed")
        # 'failed' must not be matched inside 'unfailed'
        self.assertIsNone(canary.detect_verdict("unfailed utterly"))
        # nothing verdict-like at all
        self.assertIsNone(canary.detect_verdict("all good, approved"))

    def test_classify_exit_zero_but_loud(self):
        import canary
        caught, why = canary.classify_catch(0, "REJECTED: nothing wrong",
                                            expect_pass=False)
        self.assertFalse(caught)
        self.assertIn("exit 0", why)
        self.assertIn("rejected", why)

    def test_classify_nonzero_is_catch(self):
        import canary
        caught, why = canary.classify_catch(1, "REJECTED: TODO found",
                                            expect_pass=False)
        self.assertTrue(caught)
        self.assertIn("exit 1", why)

    def test_classify_uncrunnable_gate(self):
        import canary
        caught, why = canary.classify_catch(None, "", expect_pass=False)
        self.assertFalse(caught)
        self.assertEqual(why, "gate could not be run")

    def test_strip_fault_unwraps_comments(self):
        import canary
        text = "# TODO: validate inputs\nkeep me\n// TODO: later\n"
        out = canary.strip_fault(text)
        self.assertIn("TODO", out)
        self.assertIn("keep me", out)
        self.assertNotIn("# TODO", out)
        self.assertNotIn("// TODO", out)

    def test_strip_fault_block_comment(self):
        import canary
        out = canary.strip_fault("/* TODO: secret */\nplain\n")
        self.assertIn("TODO", out)
        self.assertNotIn("/*", out)
        self.assertIn("plain", out)

    def test_looks_like_gate(self):
        import canary
        self.assertTrue(canary.looks_like_gate("check_gate.sh x"))
        self.assertFalse(canary.looks_like_gate("cook_soup.sh x"))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="canary-test-")
        self.report = os.path.join(self.tmp, "report.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- the headline behaviour: a blind gate must fail the canary ----
    def test_blind_gate_fails(self):
        """The doug_kinney case: gate approves everything, exit 1."""
        p = run_canary(["--check-cmd", fixture("blind_gate.sh"),
                        "--fault-file", os.path.join(FIX, "fault.txt"),
                        "--max-probes", "1", "--json"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        rep = json.loads(p.stdout)
        self.assertEqual(rep["verdict"], "FAIL")
        self.assertIn("exit 0", rep["runs"][-1]["why"])
        # loud_gate: says REJECTED, still exits 0 -> must not count
        p = run_canary(["--check-cmd", fixture("loud_gate.sh"),
                        "--fault-file", os.path.join(FIX, "fault.txt"),
                        "--max-probes", "1", "--json"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        rep = json.loads(p.stdout)
        self.assertIn("exit 0 but said 'rejected'", rep["runs"][-1]["why"])

    def test_working_gate_passes(self):
        p = run_canary(["--check-cmd", fixture("good_gate.sh") + " {fault}",
                        "--fault-file", os.path.join(FIX, "fault.txt"),
                        "--max-probes", "1", "--json"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rep = json.loads(p.stdout)
        self.assertEqual(rep["verdict"], "PASS")
        self.assertIn("exit 1", rep["catch_reason"])
        self.assertIn("{fault}", rep["gate"])  # gate echoed, not run raw

    def test_env_plant(self):
        p = run_canary(["--check-cmd", fixture("env_gate.sh"),
                        "--fault-file", os.path.join(FIX, "fault.txt"),
                        "--env", "CANARY_FAULT",
                        "--max-probes", "1", "--json"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rep = json.loads(p.stdout)
        self.assertEqual(rep["verdict"], "PASS")
        self.assertEqual(rep["planted_at"], "env: CANARY_FAULT")

    def test_no_strip_changes_what_the_gate_sees(self):
        """With stripping (default), quiet_gate rejects the unwrapped
        TODO; with --no-strip it stays wrapped and passes."""
        common = ["--check-cmd", fixture("quiet_gate.sh") + " {fault}",
                  "--fault-file", os.path.join(FIX, "quiet_fault.txt"),
                  "--max-probes", "1"]
        p = run_canary(common + ["--json"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = run_canary(common + ["--no-strip", "--json"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        rep = json.loads(p.stdout)
        self.assertIn("exit 0 with no rejection", rep["runs"][-1]["why"])

    def test_diff_fault(self):
        """Diff faults need patch(1). Without it, canary must say so
        (exit 2) instead of pretending to have planted anything; with
        the shim below, the full diff flow must run and catch."""
        if shutil.which("patch") is None:
            # A shim good enough for the fixture diff: apply the +lines
            # of the unified diff onto the copied tree with python.
            shim = os.path.join(self.tmp, "bin")
            os.makedirs(shim)
            with open(os.path.join(shim, "patch"), "w") as fh:
                fh.write('#!/bin/sh\n'
                         'exec ' + PY + ' ' + os.path.join(HERE, "shim_patch.py") + ' "$@"\n')
            os.chmod(os.path.join(shim, "patch"), 0o755)
            env = dict(os.environ, PATH=shim + os.pathsep
                       + os.environ.get("PATH", ""))
            def run_with_shim(argv, _env=env):
                return subprocess.run([PY, CANARY] + argv, cwd=HERE,
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE,
                                      timeout=120, env=_env)
            run_canary = run_with_shim
        p = run_canary(["--check-cmd", fixture("diff_gate.sh"),
                        "--fault-diff", os.path.join(FIX, "fault.diff"),
                        "--base-tree", os.path.join(FIX, "base_tree"),
                        "--max-probes", "1", "--json"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rep = json.loads(p.stdout)
        self.assertEqual(rep["verdict"], "PASS")

    def test_snippet_fault(self):
        p = run_canary(["--check-cmd", fixture("good_gate.sh") + " {fault}",
                        "--fault-snippet",
                        "def add(a, b):\n    return a + b  # TODO: check",
                        "--max-probes", "1", "--json"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_refine_retries_until_caught(self):
        """A working gate misses the fault while it reads 'ODOT'; the
        refiner reverses the text, so the second run plants 'TODO' and
        the gate catches it."""
        gate = os.path.join(self.tmp, "line_gate.sh")
        with open(gate, "w") as fh:
            fh.write('#!/bin/sh\n'
                     'if grep -qi "TODO" "$1" 2>/dev/null; then\n'
                     '  echo "REJECTED: TODO present"; exit 1\n'
                     'fi\n'
                     'echo APPROVED; exit 0\n')
        os.chmod(gate, 0o755)
        p = run_canary(["--check-cmd", gate + " {fault}",
                        "--fault-snippet", "ODOT: validate inputs",
                        "--refine-cmd", REVERSE_CMD_STR,
                        "--max-probes", "3", "--json"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rep = json.loads(p.stdout)
        self.assertEqual(rep["verdict"], "PASS")
        self.assertIn("refined", rep["refine"])
        self.assertEqual(len(rep["runs"]), 2)  # missed once, caught once

    def test_max_probes_bounds_gate_runs(self):
        """A gate that always misses (exit 0, no rejection words) is run
        at most --max-probes times; the refiner keeps it a no-op."""
        calls = os.path.join(self.tmp, "calls")
        gate = os.path.join(self.tmp, "count_gate.sh")
        with open(gate, "w") as fh:
            fh.write('#!/bin/sh\n'
                     'n=$(cat ' + calls + ' 2>/dev/null || echo 0)\n'
                     'echo "$((n + 1))" > ' + calls + '\n'
                     'echo APPROVED; exit 0\n')
        os.chmod(gate, 0o755)
        p = run_canary(["--check-cmd", gate,
                        "--fault-snippet", "# TODO: x",
                        "--refine-cmd", REVERSE_CMD_STR,
                        "--max-probes", "2", "--json"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        rep = json.loads(p.stdout)
        self.assertEqual(rep["verdict"], "FAIL")
        with open(calls) as fh:
            self.assertEqual(fh.read().strip(), "2")

    def test_strict_treats_uncrunnable_as_failure(self):
        """A gate that does not exist: --strict must exit 1, not 0."""
        p = run_canary(["--check-cmd", os.path.join(self.tmp, "nope.sh"),
                        "--fault-snippet", "TODO", "--max-probes", "1",
                        "--strict", "--json"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("could not be run", p.stderr.decode())

    def test_missing_gate_without_strict_is_not_a_pass(self):
        p = run_canary(["--check-cmd", os.path.join(self.tmp, "nope.sh"),
                        "--fault-snippet", "TODO", "--max-probes", "1",
                        "--json"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        rep = json.loads(p.stdout)
        self.assertEqual(rep["verdict"], "FAIL")
        self.assertIn("could not be run", rep["runs"][-1]["why"])

    def test_no_fault_is_usage_error(self):
        p = run_canary(["--check-cmd", fixture("good_gate.sh")])
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("no fault given", p.stderr.decode())

    def test_report_file_and_quiet(self):
        p = run_canary(["--check-cmd", fixture("good_gate.sh") + " {fault}",
                        "--fault-file", os.path.join(FIX, "fault.txt"),
                        "--max-probes", "1", "--report", self.report,
                        "--quiet"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout, b"")  # --quiet: nothing on stdout
        with open(self.report) as fh:
            rep = json.load(fh)
        self.assertEqual(rep["verdict"], "PASS")
        self.assertIn("exit 1", rep["catch_reason"])


if __name__ == "__main__":
    unittest.main()
