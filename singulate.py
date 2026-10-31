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
singulate - commit every changed / new file in its OWN commit, then optionally push.

Flow:  pull? -> scan changes -> preview (skip / secret guard) -> add+commit per file -> push?

Usage:
    python singulate.py                 # interactive
    python singulate.py --dry-run       # show what would happen, change nothing
    python singulate.py --prefix feat:  # "feat: updated app.py"
    python singulate.py --yes --push    # no questions (secrets/huge files are auto-skipped)

Only the Python standard library is used.
"""

import argparse
import fnmatch
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List, Optional

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
LARGE_WARN = 50 * 1024 * 1024    # warn above 50 MB
LARGE_BLOCK = 100 * 1024 * 1024  # GitHub rejects files above 100 MB -> skip them

SENSITIVE_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "*.ppk", "*.p12", "*.pfx", "*.jks",
    "*.keystore", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials.json", "client_secret*.json", "service-account*.json",
    "secrets.*", ".netrc", ".npmrc", ".pypirc",
]
SAFE_EXCEPTIONS = {".env.example", ".env.sample", ".env.template"}

USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
if USE_COLOR and os.name == "nt":
    os.system("")  # enables ANSI colors in the Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

ROOT: Optional[str] = None  # repo root, set in main()


# ----------------------------------------------------------------------------
# Small output helpers
# ----------------------------------------------------------------------------
def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def ok(msg):   print(paint(msg, "32"))
def warn(msg): print(paint(msg, "33"))
def err(msg):  print(paint(msg, "31"))
def dim(msg):  print(paint(msg, "2"))


def ask(prompt: str) -> str:
    try:
        sys.stdout.flush()
        return input(prompt).strip()
    except EOFError:
        raise KeyboardInterrupt


def yes_no(prompt: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        a = ask(f"{prompt} {hint} ").lower()
        if a == "":
            return default
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False
        print("Please answer y or n.")


# ----------------------------------------------------------------------------
# Git helpers
# ----------------------------------------------------------------------------
def git(*args: str) -> subprocess.CompletedProcess:
    """Run git quietly and capture output. --literal-pathspecs = '*' in a filename is just a '*'."""
    return subprocess.run(
        ["git", "--literal-pathspecs", *args],
        cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        encoding="utf-8", errors="surrogateescape",
    )


def git_live(*args: str) -> int:
    """Run git attached to the terminal (progress + credential prompts stay visible)."""
    return subprocess.run(["git", "--literal-pathspecs", *args], cwd=ROOT).returncode


def first_line(text: str) -> str:
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[0] if lines else "unknown error"


def find_root() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        err("git is not installed (or not on PATH).")
        sys.exit(1)
    if r.returncode != 0:
        err("This folder is not inside a git repository.")
        sys.exit(1)
    return r.stdout.strip()


def current_branch() -> str:
    r = git("rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else "HEAD"


def pick_remote() -> Optional[str]:
    names = git("remote").stdout.split()
    if not names:
        return None
    return "origin" if "origin" in names else names[0]


# ----------------------------------------------------------------------------
# Change model
# ----------------------------------------------------------------------------
@dataclass
class Change:
    kind: str                       # created | updated | deleted | renamed
    path: str
    old: Optional[str] = None       # only for renames
    staged: bool = False
    sensitive: bool = False
    size: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def paths(self) -> List[str]:
        return [self.old, self.path] if self.old else [self.path]

    @property
    def add_paths(self) -> List[str]:
        # a rename is already staged by git; "git add old" would fail, so only add the new path
        return [self.path]

    @property
    def shown(self) -> str:
        return f"{self.old} -> {self.path}" if self.old else self.path

    def message(self, prefix: str) -> str:
        return f"{prefix}{self.kind} {self.shown}"


UNMERGED = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


def is_sensitive(path: str) -> bool:
    name = os.path.basename(path).lower()
    if name in SAFE_EXCEPTIONS:
        return False
    return any(fnmatch.fnmatch(name, p) for p in SENSITIVE_PATTERNS)


def get_changes() -> List[Change]:
    r = git("status", "--porcelain=v1", "-z", "-uall")
    if r.returncode != 0:
        err("git status failed: " + first_line(r.stderr))
        sys.exit(1)

    parts = r.stdout.split("\0")
    changes: List[Change] = []
    conflicts: List[str] = []
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        old = None
        if "R" in xy or "C" in xy:      # renames/copies carry an extra "from" field
            old = parts[i] if i < len(parts) else None
            i += 1
        if xy in UNMERGED:
            conflicts.append(path)
            continue

        if xy == "??":
            kind = "created"
        elif "R" in xy:
            kind = "renamed"
        elif "D" in xy:
            kind = "deleted"
        elif "A" in xy or "C" in xy:
            kind, old = "created", None
        else:
            kind = "updated"

        c = Change(kind=kind, path=path, old=old if kind == "renamed" else None)
        c.staged = xy != "??" and xy[0] not in " ?"
        if kind != "deleted":
            c.sensitive = is_sensitive(path)
            try:
                c.size = os.path.getsize(os.path.join(ROOT, path))
            except OSError:
                c.size = 0
        changes.append(c)

    if conflicts:
        err("Merge conflicts found - resolve these first, then run again:")
        for p in conflicts:
            print("   ", p)
        sys.exit(1)
    return changes


# ----------------------------------------------------------------------------
# Steps
# ----------------------------------------------------------------------------
def pull_step(args) -> None:
    if args.pull:
        do_pull = True
    elif args.no_pull or args.yes:
        do_pull = False
    else:
        do_pull = yes_no("Pull first?", default=False)
    if not do_pull:
        return
    if args.dry_run:
        dim("[dry-run] would run: git pull --ff-only")
        return
    print("Pulling...")
    if git_live("pull", "--ff-only") != 0:
        err("Pull failed (see message above). If branches diverged, run 'git pull --rebase' yourself.")
        if args.yes or not yes_no("Continue without pulling?", default=False):
            sys.exit(1)


def size_text(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def show_list(changes: List[Change]) -> None:
    symbols = {"created": ("+", "32"), "updated": ("~", "33"),
               "deleted": ("-", "31"), "renamed": (">", "36")}
    print()
    for n, c in enumerate(changes, 1):
        sym, color = symbols[c.kind]
        line = f"{n:>3}. {paint(sym, color)} {c.kind:<7} {c.shown}"
        for note in c.notes:
            line += "  " + paint(f"({note})", "33")
        print(line)
    print()


def review(changes: List[Change], args) -> List[Change]:
    """Flag problems, let the user exclude files. Returns the final list to commit."""
    for c in changes:
        if c.staged:
            c.notes.append("already staged")
        if c.sensitive:
            c.notes.append("SENSITIVE?")
        if c.size >= LARGE_WARN:
            c.notes.append(f"large: {size_text(c.size)}")

    # files GitHub would reject anyway are always skipped
    blocked = [c for c in changes if c.size >= LARGE_BLOCK]
    if blocked:
        warn("Skipping files over 100 MB (GitHub would reject them):")
        for c in blocked:
            print("   ", c.shown, f"({size_text(c.size)})")
        changes = [c for c in changes if c not in blocked]

    show_list(changes)
    if any(c.staged for c in changes):
        dim("Note: staged files still get their own commit; a file with both staged and "
            "unstaged edits is committed whole.")

    sensitive = [c for c in changes if c.sensitive]
    if sensitive:
        warn("These files look like secrets/keys. Pushing them to GitHub can leak credentials:")
        for c in sensitive:
            print("   ", c.shown)
        if args.yes:
            choice = "e"
        else:
            while True:
                choice = ask("[e] exclude them (default)  [i] include anyway  [a] abort: ").lower() or "e"
                if choice in ("e", "i", "a"):
                    break
        if choice == "a":
            return []
        if choice == "e":
            changes = [c for c in changes if not c.sensitive]
            ok(f"Excluded {len(sensitive)} sensitive file(s).")

    if args.yes:
        return changes

    while changes:
        show_list(changes)
        ans = ask(f"Commit these {len(changes)} file(s), one commit each? "
                  "[Y]es / [n]o / skip numbers (e.g. 's 2 5'): ").lower()
        if ans in ("", "y", "yes"):
            return changes
        if ans in ("n", "no"):
            return []
        if ans.split()[0] in ("s", "skip"):
            try:
                drop = {int(x) for x in ans.split()[1:]}
            except ValueError:
                print("Use numbers, e.g. 's 2 5'.")
                continue
            if not drop or any(d < 1 or d > len(changes) for d in drop):
                print("Those numbers aren't in the list.")
                continue
            changes = [c for n, c in enumerate(changes, 1) if n not in drop]
        else:
            print("Type y, n, or 's 2 5'.")
    return changes


def commit_all(changes: List[Change], args, done: list, failed: list) -> None:
    prefix = (args.prefix.rstrip() + " ") if args.prefix else ""
    for c in changes:
        msg = c.message(prefix)
        if args.dry_run:
            dim(f"[dry-run] git add -- {' '.join(c.add_paths)}")
            dim(f"[dry-run] git commit -m \"{msg}\" -- {' '.join(c.paths)}")
            done.append(c)
            continue
        r = git("add", "--", *c.add_paths)
        if r.returncode != 0:
            failed.append((c, first_line(r.stderr)))
            err(f"✘ {c.kind} {c.shown}: {first_line(r.stderr)}")
            continue
        # "-- paths" = commit ONLY these files, even if other things are staged
        r = git("commit", "-m", msg, "--", *c.paths)
        if r.returncode != 0:
            reason = first_line(r.stderr or r.stdout)
            failed.append((c, reason))
            err(f"✘ {c.kind} {c.shown}: {reason}")
            continue
        done.append(c)
        ok(f"✔ {c.kind} {c.shown}")


def push_step(args, committed: int) -> Optional[str]:
    """Returns a description of where it pushed, or None."""
    if committed == 0:
        return None
    branch = current_branch()
    if args.dry_run:
        dim("[dry-run] would ask whether to push")
        return None
    if args.push:
        choice = "y"
    elif args.no_push or args.yes:
        return None
    else:
        while True:
            choice = ask(f"Push?  [n] no   [y] current branch ('{branch}')   "
                         "[A] another branch : ").lower() or "n"
            if choice in ("n", "y", "a"):
                break
            print("Type n, y or A.")
    if choice == "n":
        return None

    remote = pick_remote()
    if not remote:
        err("No remote configured. Add one with: git remote add origin <url>")
        return None

    if choice == "y":
        if branch == "HEAD":
            err("You're on a detached HEAD. Re-run and choose A to name a branch.")
            return None
        has_upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").returncode == 0
        cmd = ["push"] if has_upstream else ["push", "-u", remote, branch]
        target = branch
    else:
        name = ask("Branch name: ")
        if not name or git("check-ref-format", "--branch", name).returncode != 0:
            err("That isn't a valid branch name.")
            return None
        cmd = ["push", remote, f"HEAD:refs/heads/{name}"]
        target = name

    print(f"Pushing to {remote}/{target}...")
    if git_live(*cmd) != 0:
        err("Push failed (see above). If the remote has newer commits, pull first "
            "(git pull --rebase) and run again. Force-push is never used by this tool.")
        return None
    ok(f"Pushed to {remote}/{target}")
    return f"{remote}/{target}"


def summary(done, failed, skipped, pushed, dry=False) -> None:
    print()
    print(paint("Summary" + (" (dry run)" if dry else ""), "1"))
    counts = {}
    for c in done:
        counts[c.kind] = counts.get(c.kind, 0) + 1
    parts = [f"{n} {k}" for k, n in counts.items()]
    print(f"  Committed: {len(done)}" + (f"  ({', '.join(parts)})" if parts else ""))
    if skipped:
        print(f"  Skipped:   {skipped}")
    if failed:
        print(paint(f"  Failed:    {len(failed)}", "31"))
        for c, why in failed:
            print(f"     - {c.shown}: {why}")
    if pushed:
        print(f"  Pushed to: {pushed}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Commit each changed file separately, then optionally push.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--pull", action="store_true", help="pull without asking")
    g.add_argument("--no-pull", action="store_true", help="don't pull, don't ask")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--push", action="store_true", help="push current branch without asking")
    g.add_argument("--no-push", action="store_true", help="don't push, don't ask")
    p.add_argument("--yes", "-y", action="store_true",
                   help="no questions: secrets & >100MB files are auto-skipped, no pull/push unless flagged")
    p.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    p.add_argument("--prefix", default="", help='message prefix, e.g. "feat:"')
    return p.parse_args()


def main() -> int:
    global ROOT
    args = parse_args()
    ROOT = find_root()
    done, failed = [], []
    skipped = 0
    pushed = None

    try:
        if git("rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
            err("A merge is in progress. Finish or abort it first.")
            return 1

        pull_step(args)

        changes = get_changes()
        if not changes:
            ok("Nothing to commit - working tree clean.")
            return 0

        selected = review(changes, args)
        skipped = len(changes) - len(selected)
        if not selected:
            print("Nothing selected. Bye.")
            return 0

        commit_all(selected, args, done, failed)
        pushed = push_step(args, len(done))
    except KeyboardInterrupt:
        print("\n\nInterrupted - stopping safely.")
        summary(done, failed, skipped, pushed, args.dry_run)
        return 130

    summary(done, failed, skipped, pushed, args.dry_run)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
