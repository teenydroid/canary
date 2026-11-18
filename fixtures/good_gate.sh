#!/bin/sh
# A gate that still works: rejects a planted TODO fault.
grep -q "TODO" "${1:-fault.txt}" 2>/dev/null && { echo "REJECTED: TODO found in fault.txt"; exit 1; }
echo "APPROVED"
exit 0
