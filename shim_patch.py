#!/usr/bin/env python3
"""A tiny stand-in for patch(1), used only by the tests when the real
one is not installed. Understands exactly the shape of fixture diff
canary feeds it: `patch -p1 --batch -i FILE` run inside the copied tree.

It applies the + lines of a unified diff whose paths are a/... b/...
onto the tree, and exits 0. It is not a general patch implementation.
"""

import os
import sys


def main(argv):
    diff_path = None
    args = argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "-i":
            diff_path = args[i + 1]
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            i += 1
    if not diff_path:
        print("shim_patch: no -i file", file=sys.stderr)
        return 2
    with open(diff_path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    target = None
    for line in lines:
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target.startswith("b/"):
                target = target[2:]
        elif line.startswith("+") and not line.startswith("+++") \
                and target:
            path = os.path.join(os.getcwd(), target)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line[1:] + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
