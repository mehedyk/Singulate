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

import subprocess


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

    run("add", ".")                      # stages EVERYTHING (also .env files - be careful!)
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
    main()
