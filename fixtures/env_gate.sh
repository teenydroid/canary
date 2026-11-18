#!/bin/sh
# A gate that reads the sample from CANARY_FAULT in the environment.
printf '%s\n' "$CANARY_FAULT" | grep -q "TODO" && { echo "REJECTED: TODO in env sample"; exit 1; }
echo "APPROVED"
exit 0
