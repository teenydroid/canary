# canary

A stdlib-only Python 3 CLI that proves a review gate can still catch
something, by feeding it a fault it must reject.

A review gate (a check script, a reviewer bot, a lint step) can fail in
the worst possible way: silently. An override flag gets flipped, a
config changes, and the gate keeps running while approving everything.
canary catches that. Each run plants a sample that the gate MUST
reject, runs the gate against it, and exits 0 only if the gate really
rejected it. If the gate has gone blind, canary goes red.

## What counts as a catch

A non-zero exit from the gate. Rejection words in the output with exit 0
do NOT count: a gate that prints "REJECTED" and approves anyway is
exactly the failure canary exists to catch, and it reports that as a
miss (`exit 0 but said 'rejected'`).

## How to run it

```sh
# the gate still works?
./canary.py --check-cmd "./my_gate.sh {fault}" --fault-file fault.txt

# a gate that has gone blind: exit 1, with the reason
./canary.py --check-cmd ./blind_gate.sh --fault-file fault.txt
```

`--check-cmd` is the command that runs your check or review gate. It
runs inside a throwaway tree with the fault planted in it; put `{fault}`
where the planted file's path should go. Without `{fault}`, canary warns
that the gate may not see the fault at all.

Exit codes: `0` the gate still catches faults; `1` it did not (or, with
`--strict`, could not be run at all); `2` canary was used wrong.

## Planting the fault

Three sources, tried in order, first non-empty wins; all repeatable:

```sh
--fault-file PATH    # a file whose content is the fault
--fault-diff PATH    # a unified diff, applied onto --base-tree
--fault-snippet S    # a raw snippet
```

By default canary unwraps comment markers (`//`, `#`, `/* */`, triple
quotes, `<!-- -->`) from the sample, so a fault hidden behind a comment
is still what the gate is fed. `--no-strip` plants the sample exactly as
given.

To plant into an environment variable instead of the tree (for gates
that read the sample from the environment):

```sh
./canary.py --check-cmd ./env_gate.sh --fault-file fault.txt \
    --env REVIEW_SAMPLE
```

## When the gate misses

By default canary retries up to `--max-probes` times (default 3). With
`--refine-cmd`, each retry first asks a rewriter to make the fault less
obvious: the command gets the fault on stdin and its stdout replaces it.

```sh
./canary.py --check-cmd "./gate {fault}" --fault-snippet "TODO: x" \
    --refine-cmd "my_rewriter --subtler"
```

A gate that cannot be run at all (missing binary, timeout) is reported
as a miss, never as a pass. `--strict` makes that case exit 1 directly.

## Output

Human-readable one-liner on stderr by default. `--json` prints the full
report on stdout; `--report FILE` writes it to a file; `--quiet` keeps
stdout clean for the report file alone.

```json
{
  "verdict": "FAIL",
  "gate": "./blind_gate.sh",
  "provenance": "fault file: fault.txt",
  "runs": [{"attempt": 1, "rc": 0, "caught": false,
            "why": "exit 0 with no rejection"}],
  "miss_reason": "exit 0 with no rejection"
}
```

## Fixtures

`fixtures/` holds toy gates for the tests: `good_gate.sh` still works,
`blind_gate.sh` approves everything, `loud_gate.sh` says REJECTED and
still exits 0, `quiet_gate.sh` only rejects faults stated outside a
comment, `env_gate.sh` reads the sample from `CANARY_FAULT`,
`diff_gate.sh` checks a whole tree, and `refine_reverse.sh` is a toy
rewriter.

## Tests

```sh
python3 -m unittest -v
```

19 tests, all green. When `patch(1)` is not installed, the diff test
uses a small shim (`shim_patch.py`) so the diff flow is still exercised
end to end.

## Limits

- A catch means a non-zero exit; a gate whose only signal is its output
  text (and that exits 0) cannot be judged by canary.
- `--fault-diff` needs `patch(1)`.
- The rewriter in `--refine-cmd` is whatever you point it at; canary
  ships none.
