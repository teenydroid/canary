#!/bin/sh
# A gate for diff faults: rejects if the TODO fault is anywhere in the
# tree it is run in (canary runs it inside the materialised diff tree).
grep -rq "TODO: validate inputs" . && { echo "REJECTED: TODO fault present in tree"; exit 1; }
echo "APPROVED"
exit 0
