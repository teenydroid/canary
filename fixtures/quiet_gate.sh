#!/bin/sh
# A gate that only rejects when the fault is stated outside a comment.
# Used to prove that canary's default comment-unwrapping changes what
# the gate sees, and that --no-strip keeps the wrapper.
sample="$1"
if grep -v '^[[:space:]]*#' "$sample" 2>/dev/null | grep -qi 'todo'; then
    echo "REJECTED: todo stated outside a comment"
    exit 1
fi
echo "APPROVED"
exit 0
