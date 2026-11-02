#!/usr/bin/env python3
# Singulate - one file, one commit.
# Copyright (C) 2026 mehedyk (https://mehedy.netlify.app)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. It is distributed WITHOUT ANY WARRANTY;
# see the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.
"""
simple_push - the beginner version.
Pull (optional) -> git add . -> commit with ONE message -> git push.
Run it inside your project folder:  python simple_push.py
"""

import os
import re
import subprocess
import sys

TOOL_NAME_RX = re.compile(
    r"^(singulate|singulate[_-]pro|simple[_-]push|autocommit)(-(windows|macos|linux))?(\.py|\.exe)?$", re.I)


def frozen():
    return bool(getattr(sys, "frozen", False))


def self_path():
    return os.path.realpath(sys.executable if frozen() else __file__)


def own_files():
    """This tool's own (untracked) files inside the repo - we never commit ourselves."""
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
    me = self_path()
    here = os.path.dirname(me)
    try:
        rel = os.path.relpath(here, os.path.realpath(top))
    except ValueError:
        return []
    if rel == ".." or rel.startswith(".." + os.sep):
        return []
    try:
        names = {n for n in os.listdir(here) if TOOL_NAME_RX.match(n)}
    except OSError:
        names = set()
    names.add(os.path.basename(me))
    found = []
    for name in names:
        path = name if rel == "." else f"{rel}/{name}".replace(os.sep, "/")
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "--", path],
                                 capture_output=True, text=True, cwd=top).returncode == 0
        if os.path.exists(os.path.join(top, path)) and not tracked:
            found.append(path)
    return found


def pause_if_double_clicked():
    """A Windows .exe opened by double-click closes instantly; keep the window open to read the result."""
    if not (frozen() and os.name == "nt"):
        return
    try:
        import ctypes
        procs = (ctypes.c_uint * 8)()
        if ctypes.windll.kernel32.GetConsoleProcessList(procs, 8) <= 2:
            input("\nPress Enter to close this window...")
    except Exception:
        pass


def run(*cmd):
    """Run a git command and show its output."""
    return subprocess.run(["git", *cmd]).returncode


def has_changes():
    out = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    return bool(out.stdout.strip())


def main():
    if run("rev-parse", "--is-inside-work-tree") != 0:
        print("This folder is not a git repository.")
        return

    if input("Pull first? (y/n): ").strip().lower() == "y":
        if run("pull") != 0:
            print("Pull failed - fix that first, then run again.")
            return

    if not has_changes():
        print("Nothing to commit.")
        return

    run("status", "--short")
    msg = input("Commit message (Enter = 'update'): ").strip() or "update"

    skip = [f":(top,exclude,literal){p}" for p in own_files()]
    run("add", "--", ".", *skip)         # stages EVERYTHING except this tool (also .env - be careful!)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        print("Nothing to commit (only Singulate's own files changed, and those are skipped).")
        return
    if run("commit", "-m", msg) != 0:
        print("Commit failed.")
        return

    if run("push") != 0:
        # most common reason: the branch has no upstream yet
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        print(f"Plain push failed. Trying: git push -u origin {branch}")
        run("push", "-u", "origin", branch)


if __name__ == "__main__":
    try:
        main()
    finally:
        pause_if_double_clicked()
