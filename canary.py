#!/usr/bin/env python3
"""canary: prove a review gate can still catch a fault.

Feed it the command of a check or review gate and a sample that the gate
MUST reject (a planted fault). Every run stages the fault, runs the gate
against it and reports PASS only if the gate actually rejects it.

Planted-fault sources (repeatable, tried in order, first non-empty wins):
  --fault-file PATH   a file whose content is the fault
  --fault-diff PATH   a unified diff applied to a base tree (--base-tree)
  --fault-snippet S   a raw snippet

Where the gate is run: inside a throwaway tree. Pass {fault} in
--check-cmd for the planted file's path, or --env VAR to plant the fault
text into an environment variable instead.

A catch means a non-zero gate exit. Rejection words in output with exit 0
do not count: a gate that says "REJECTED" but approves is exactly the
failure canary exists to catch. By default comment wrappers (//, #,
slash-star, triple quotes, HTML comments) are unwrapped from the sample
so a fault hidden behind a comment is still what the gate is fed;
--no-strip plants the sample exactly as given. --refine-cmd asks a
rewriter to make the fault less obvious between attempts; --max-probes
bounds the attempts.

Exit codes: 0 gate still catches faults; 1 it did not (or, with --strict,
could not run at all); 2 canary used wrong; 3 unused.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

PROG = "canary"
STRICT_NOTE = "probe could not be run; with --strict that is a gate failure"

DEFAULT_TOKENS = [
    "reject", "rejected", "blocking", "blocked", "veto", "vetoed",
    "fail", "failed", "failure", "error", "violation", "violations",
    "must not", "forbidden", "not allowed", "critical", "fatal",
]

# (pattern, marker prefix, marker suffix); used to unwrap comments.
COMMENT_PATTERNS = [
    (re.compile(r"/\*.*?\*/", re.DOTALL), "/*", "*/"),
    (re.compile(r"//[^\n]*"), "//", ""),
    (re.compile(r"#[^\n]*"), "#", ""),
    (re.compile(r'""".*?"""', re.DOTALL), '"""', '"""'),
    (re.compile(r"<!--.*?-->", re.DOTALL), "<!--", "-->"),
]

LEADING_WORDS = [
    "check", "verify", "gate", "review", "audit", "inspect",
    "test", "scan", "lint", "vet", "warn",
]


def fail(msg, code=2):
    print(f"{PROG}: {msg}", file=sys.stderr)
    raise SystemExit(code)


def note(msg):
    print(f"{PROG}: {msg}", file=sys.stderr)


def run_probe(check_cmd, cwd, timeout, env=None):
    """Run the gate once. Returns (rc, output); rc is None if it could
    not be run at all or timed out."""
    try:
        p = subprocess.run(check_cmd, cwd=cwd, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, ""
    except OSError as exc:
        note(f"probe error: {exc}")
        return None, ""
    return p.returncode, p.stdout.decode("utf-8", "replace")


def looks_like_gate(cmd_text):
    words = cmd_text.lower().split()
    if not words:
        return False
    first = os.path.basename(words[0])
    return any(w in first for w in LEADING_WORDS)


def detect_verdict(text):
    """Find the verdict word the gate used, whole words only."""
    low = (text or "").lower()
    for tok in DEFAULT_TOKENS:
        if re.search(r"\b" + re.escape(tok) + r"\b", low):
            return tok
    return None


def classify_catch(rc, out, expect_pass):
    """Decide if the gate did what it must with the fault.
    Default: the fault must be rejected (non-zero exit). With
    --expect-pass the sample should pass instead."""
    out = out or ""
    if rc is None:
        return False, "gate could not be run"
    verdict = detect_verdict(out)
    said = f", said '{verdict}'" if verdict else ""
    if rc == 0:
        if expect_pass:
            return True, "exit 0"
        if verdict:
            return False, f"exit 0 but said '{verdict}'"
        return False, "exit 0 with no rejection"
    if expect_pass:
        return False, f"exit {rc}{said}"
    return True, f"exit {rc}{said}"


def strip_fault(text):
    """Unwrap comment markers, keeping the text they hid."""
    for pat, prefix, suffix in COMMENT_PATTERNS:
        def repl(m, prefix=prefix, suffix=suffix):
            inner = m.group(0)[len(prefix):]
            if suffix and inner.endswith(suffix):
                inner = inner[:-len(suffix)]
            if not suffix:  # line comments: drop the leftover indent
                inner = inner.lstrip()
            return inner
        text = pat.sub(repl, text)
    return text


def refine_step(text, cmd_argv):
    """One pass through the rewriter: fault on stdin, replacement on
    stdout. Returns (new_text, note); the text is unchanged when the
    rewriter fails, refuses or returns a no-op."""
    try:
        p = subprocess.run(cmd_argv, input=text.encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=20)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return text, f"refine failed: {exc}"
    if p.returncode != 0:
        return text, f"refine exited {p.returncode}"
    out = p.stdout.decode("utf-8", "replace")
    if not out.strip() or out == text:
        return text, "refine was a no-op or empty"
    return out, f"refined (attempt used a rewritten fault)"


def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog=PROG,
        description="Feed a planted fault to a review gate and check it "
                    "still rejects the fault.")
    ap.add_argument("--check-cmd", required=True,
                    help="command that runs the check or review gate; "
                         "runs inside the planted tree; use {fault} where "
                         "the planted file's path should go")
    ap.add_argument("--fault-file", action="append", default=None,
                    help="file whose content is the planted fault "
                         "(repeatable)")
    ap.add_argument("--fault-diff", action="append", default=None,
                    help="unified diff planted as fault (needs "
                         "--base-tree, repeatable)")
    ap.add_argument("--base-tree", default=None,
                    help="tree to apply --fault-diff onto")
    ap.add_argument("--fault-snippet", action="append", default=None,
                    help="raw snippet planted as fault (repeatable)")
    ap.add_argument("--no-strip", dest="strip", action="store_false",
                    default=True,
                    help="plant the sample exactly as given; default "
                         "unwraps //, #, /* */, triple quotes and "
                         "<!-- --> comment wrappers")
    ap.add_argument("--expect-pass", action="store_true",
                    help="invert: the sample should PASS the gate")
    ap.add_argument("--env", action="append", default=None, metavar="VAR",
                    help="plant the fault text into this env var instead "
                         "of the tree (repeatable)")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable report on stdout")
    ap.add_argument("--quiet", action="store_true",
                    help="only the exit code and the report file")
    ap.add_argument("--strict", action="store_true",
                    help="a gate that cannot be run is a failure, not a "
                         "pass")
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="seconds per gate run (default 60)")
    ap.add_argument("--refine-cmd", default=None,
                    help="command run with the fault on stdin, whose "
                         "stdout replaces the fault, to make it less "
                         "obvious between attempts")
    ap.add_argument("--max-probes", type=int, default=3,
                    help="max gate runs (default 3)")
    ap.add_argument("--report", default=None,
                    help="write a JSON report to this file")
    return ap.parse_args(argv)


def materialise_fault(args, root):
    """Return (payload, provenance, kind); kind is "text" or "tree".
    Diffs are applied onto a copy of the base tree inside root."""
    for path in (args.fault_file or []):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read()
        except OSError as exc:
            fail(f"cannot read fault file {path}: {exc}")
        if data.strip():
            return data, f"fault file: {path}", "text"
    for path in (args.fault_diff or []):
        if not args.base_tree:
            fail("--fault-diff needs --base-tree")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                fh.read()
        except OSError as exc:
            fail(f"cannot read diff {path}: {exc}")
        tree = apply_fault_diff(args.base_tree, path, root)
        return tree, f"fault diff {path} applied onto {args.base_tree}", "tree"
    for snip in (args.fault_snippet or []):
        if snip.strip():
            return snip, "--fault-snippet", "text"
    fail("no fault given: pass --fault-file, --fault-diff or "
         "--fault-snippet")


def apply_fault_diff(base, diff_path, root):
    """Apply diff_path onto a copy of base inside root; return the tree."""
    if shutil.which("patch") is None:
        fail("patch(1) is not installed; --fault-diff needs it")
    work = os.path.join(root, "box")
    if os.path.isdir(base):
        shutil.copytree(base, work, dirs_exist_ok=True)
    else:
        shutil.copy2(base, work)
    cwd = work if os.path.isdir(base) else os.path.dirname(work)
    p = subprocess.run(["patch", "-p1", "--batch", "-i", diff_path],
                       cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    if p.returncode != 0:
        fail("could not apply --fault-diff: "
             + p.stdout.decode("utf-8", "replace").strip())
    return work


def resolve_program(argv):
    """Make path-like arguments (the program itself, and any argument
    that names an existing file or directory) absolute, so the probe can
    run from inside the planted tree. Flags and bare words are kept."""
    out = []
    for i, tok in enumerate(argv):
        if (i == 0 or "/" in tok) and not os.path.isabs(tok) \
                and os.path.exists(tok):
            out.append(os.path.abspath(tok))
        else:
            out.append(tok)
    return out


def find_fault_files(root):
    """Sorted files that carry content, smallest first."""
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                with open(full, "r", encoding="utf-8",
                          errors="replace") as fh:
                    body = fh.read()
            except OSError:
                continue
            if body:
                hits.append((len(body), full))
    hits.sort()
    return [p for _n, p in hits]


def plant_into_tree(root, text):
    """Choose the file the fault belongs in and write it there."""
    cands = find_fault_files(root)
    target = cands[0] if cands else os.path.join(root, "fault.txt")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(text)
    return target


def build_probe(args, kind, fault, box, target):
    """One shot of staging + command building. Returns (argv, cwd, env,
    planted_at)."""
    env = dict(os.environ)
    if kind == "tree":
        return args._probe_argv, fault, env, "materialised diff tree"
    if args.env:
        for var in args.env:
            env[var] = fault
        return args._probe_argv, box, env, f"env: {', '.join(args.env)}"
    if target is None:
        planted = plant_into_tree(box, fault)
    else:
        planted = target
        with open(planted, "w", encoding="utf-8") as fh:
            fh.write(fault)
    if "{fault}" in args.check_cmd:
        argv = resolve_program(
            shlex.split(args.check_cmd.replace("{fault}", planted)))
    else:
        argv = args._probe_argv
        note("gate command has no {fault} and no --env: it may not see "
             "the planted fault at all")
    return argv, box, env, planted


def main(argv=None):
    args = parse_args(argv)
    check_argv = shlex.split(args.check_cmd)
    if not check_argv:
        fail("--check-cmd is empty")
    args._probe_argv = resolve_program(check_argv)
    if not looks_like_gate(" ".join(check_argv)):
        note(f"'{args.check_cmd}' does not look like a gate (no "
             "check/verify/gate/review word in it)")

    root = tempfile.mkdtemp(prefix="canary-box-")
    box = os.path.join(root, "box")
    os.makedirs(box)
    refine_argv = shlex.split(args.refine_cmd) if args.refine_cmd else None
    try:
        fault, provenance, kind = materialise_fault(args, root)
        if args.env and kind != "text":
            fail("--env needs a text fault, not a diff")
        if kind == "text":
            if args.strip:
                fault = strip_fault(fault)
            else:
                note("--no-strip: planting the sample exactly as given")
            refine_note = "unrefined"
        else:
            refine_note = "not refined: tree payload"

        report = {
            "verdict": "FAIL",
            "gate": args.check_cmd,
            "provenance": provenance,
            "refine": refine_note,
            "runs": [],
        }
        target = None
        why = ""
        max_probes = max(1, args.max_probes)
        for attempt in range(1, max_probes + 1):
            run_argv, cwd, env, planted = build_probe(
                args, kind, fault, box, target)
            target = planted if kind == "text" else target
            report["planted_at"] = str(planted)
            rc, out = run_probe(run_argv, cwd, args.timeout, env)
            if rc is None and args.strict:
                fail(STRICT_NOTE, 1)
            caught, why = classify_catch(rc, out, args.expect_pass)
            report["runs"].append({
                "attempt": attempt, "rc": rc, "caught": caught, "why": why,
                "output_tail": (out or "").strip().splitlines()[-3:],
            })
            if caught:
                report["verdict"] = "PASS"
                report["catch_reason"] = why
                break
            if kind != "text" or refine_argv is None \
                    or attempt == max_probes:
                report["miss_reason"] = why
                break
            fault, refine_note = refine_step(fault, refine_argv)
            report["refine"] = refine_note
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    if args.json:
        print(json.dumps(report, indent=2))
    elif not args.quiet:
        why = report.get("catch_reason") or report.get("miss_reason") or ""
        note(f'{report["verdict"]}: {why} '
             f'(attempts: {len(report["runs"])}; '
             f'planted: {report.get("planted_at", "?")})')
    sys.exit(0 if report["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
