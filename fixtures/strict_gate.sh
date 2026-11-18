#!/bin/sh
grep -q "TODO" "$CANARY_FAULT" 2>/dev/null && { echo "REJECTED"; exit 1; }
echo APPROVED
