#!/bin/sh
grep -q "TODO" "$1" 2>/dev/null && { echo "REJECTED: TODO found"; exit 1; }
echo "APPROVED"
exit 0
