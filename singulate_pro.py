#!/usr/bin/env python3
# Singulate Pro - one file, one commit, even when git gets complicated.
# Copyright (C) 2026 mehedyk (https://mehedy.netlify.app)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. It is distributed WITHOUT ANY WARRANTY;
# see the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.
"""
Singulate Pro - the expert edition.

Everything the normal version does, plus:
  * login check (GitHub CLI / SSH / HTTPS token) with "keep this account or switch?"
  * guided merge / rebase conflict resolution (never silent)
  * detects merge, rebase, cherry-pick, revert already in progress
  * diverged pulls, rejected pushes, detached HEAD, empty repos, missing identity
  * stale index.lock, failing hooks, GPG-signing failures
  * content secret scan (AWS keys, tokens, private keys ...)
  * submodules, symlinks, mode-only changes, case-only renames, nested repos
  * huge change sets (group by folder), undo of the last run, interrupted-run memory
  * --doctor report

Usage:
    python singulate_pro.py                 # interactive
    python singulate_pro.py --doctor        # read-only report of repo + login state
    python singulate_pro.py --undo          # undo the last run (only if nothing was pushed)
    python singulate_pro.py --dry-run       # show what would happen
    python singulate_pro.py --switch-account
    python singulate_pro.py -y --push       # no questions (never resolves conflicts, never logs in)

Only the Python standard library is used.
"""

import argparse
import fnmatch
import getpass
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

VERSION = "1.0.0"

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
LARGE_WARN = 50 * 1024 * 1024
LARGE_BLOCK = 100 * 1024 * 1024
SCAN_MAX = 2 * 1024 * 1024          # don't content-scan files above 2 MB
GROUP_THRESHOLD = 200               # offer folder-grouping above this many files

SENSITIVE_NAMES = [
    ".env", ".env.*", "*.pem", "*.key", "*.ppk", "*.p12", "*.pfx", "*.jks",
    "*.keystore", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials.json", "client_secret*.json", "service-account*.json",
    "secrets.*", ".netrc", ".npmrc", ".pypirc",
]
SAFE_NAMES = {".env.example", ".env.sample", ".env.template"}

SECRET_PATTERNS = [
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("GitHub token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Stripe live key", re.compile(r"\bsk_live_[0-9a-zA-Z]{20,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("hard-coded secret (possible)", re.compile(
        r"""(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*['"][^'"\s]{8,}['"]""")),
]

USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
if USE_COLOR and os.name == "nt":
    os.system("")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


# ----------------------------------------------------------------------------
# Output / input helpers
# ----------------------------------------------------------------------------
def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def ok(msg):   print(paint(msg, "32"))
def warn(msg): print(paint(msg, "33"))
def err(msg):  print(paint(msg, "31"))
def dim(msg):  print(paint(msg, "2"))
def bold(msg): print(paint(msg, "1"))


def ask(prompt: str) -> str:
    try:
        sys.stdout.flush()
        return input(prompt).strip()
    except EOFError:
        raise KeyboardInterrupt


def ask_secret(prompt: str) -> str:
    try:
        return getpass.getpass(prompt).strip()
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


def choose(prompt: str, valid: str, default: Optional[str] = None) -> str:
    """Ask for a single-letter/number choice. Returns the lowercase choice."""
    while True:
        a = ask(prompt).lower()
        if a == "" and default:
            return default
        if len(a) == 1 and a in valid:
            return a
        print(f"Please type one of: {', '.join(valid)}")


# ----------------------------------------------------------------------------
# Global run state
# ----------------------------------------------------------------------------
@dataclass
class State:
    root: str = ""
    git_dir: str = ""
    verbose: bool = False
    env: Dict[str, str] = field(default_factory=dict)  # extra env for every git call
    gpg_off: bool = False        # commit unsigned for this run
    push_blocked: bool = False   # not logged in / user chose to skip pushing
    login_deferred: bool = False  # "let git ask me when I push"
    remote: Optional["Remote"] = None
    login_line: str = ""
    conflicts_resolved: int = 0
    notes: List[str] = field(default_factory=list)


S = State()


# ----------------------------------------------------------------------------
# Git wrappers
# ----------------------------------------------------------------------------
@dataclass
class R:
    rc: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return (self.out or "") + (self.err or "")


def _ssh_base() -> str:
    if S.env.get("GIT_SSH_COMMAND"):
        return S.env["GIT_SSH_COMMAND"]
    if os.environ.get("GIT_SSH_COMMAND"):
        return os.environ["GIT_SSH_COMMAND"]
    r = subprocess.run(["git", "config", "--get", "core.sshCommand"], capture_output=True,
                       text=True, cwd=S.root or None, stdin=subprocess.DEVNULL)
    return r.stdout.strip() or "ssh"


def _env(extra: Optional[Dict[str, str]] = None, noninteractive: bool = False) -> Dict[str, str]:
    env = os.environ.copy()
    env.update(S.env)
    if noninteractive:
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        env["GH_PROMPT_DISABLED"] = "1"
        for k in ("GIT_ASKPASS", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"):
            env.pop(k, None)
        base = _ssh_base()
        if os.path.basename(shlex.split(base)[0]).lower().startswith("ssh"):
            base += " -o BatchMode=yes -o ConnectTimeout=10"
        env["GIT_SSH_COMMAND"] = base
    if extra:
        env.update(extra)
    return env


def git(*args: str, input: Optional[str] = None, noninteractive: bool = False,
        timeout: Optional[int] = None, env: Optional[Dict[str, str]] = None) -> R:
    """Run git quietly and capture output (literal pathspecs: '*' in a name is just '*')."""
    cmd = ["git", "--literal-pathspecs", *args]
    if S.verbose:
        dim("$ " + " ".join(shlex.quote(a) for a in cmd))
    kw = dict(cwd=S.root or None, capture_output=True, text=True, encoding="utf-8",
              errors="surrogateescape", env=_env(env, noninteractive), timeout=timeout)
    if input is not None:
        kw["input"] = input
    else:
        kw["stdin"] = subprocess.DEVNULL
    try:
        p = subprocess.run(cmd, **kw)
    except subprocess.TimeoutExpired:
        return R(124, "", "timeout")
    except FileNotFoundError:
        err("git is not installed (or not on PATH).")
        sys.exit(1)
    return R(p.returncode, p.stdout, p.stderr)


def git_bytes(*args: str) -> Tuple[int, bytes]:
    cmd = ["git", "--literal-pathspecs", *args]
    p = subprocess.run(cmd, cwd=S.root or None, capture_output=True, stdin=subprocess.DEVNULL, env=_env())
    return p.returncode, p.stdout


def git_live(*args: str, env: Optional[Dict[str, str]] = None) -> int:
    """Run git attached to the terminal (progress + prompts stay visible)."""
    cmd = ["git", "--literal-pathspecs", *args]
    if S.verbose:
        dim("$ " + " ".join(shlex.quote(a) for a in cmd))
    return subprocess.run(cmd, cwd=S.root or None, env=_env(env)).returncode


def first_line(text: str) -> str:
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[0] if lines else "unknown error"


def clip(text: str, n: int = 3) -> str:
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return "\n".join("    " + l for l in lines[:n])


def init_repo_context() -> None:
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel", "--absolute-git-dir"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        err("git is not installed (or not on PATH).")
        sys.exit(1)
    if r.returncode != 0:
        err("This folder is not inside a git repository.")
        sys.exit(1)
    lines = r.stdout.splitlines()
    S.root, S.git_dir = lines[0].strip(), lines[1].strip()


def git_version() -> Tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", git("--version").out)
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


# ----------------------------------------------------------------------------
# Repo state helpers
# ----------------------------------------------------------------------------
def head_sha() -> Optional[str]:
    r = git("rev-parse", "-q", "--verify", "HEAD")
    return r.out.strip() if r.rc == 0 else None


def current_branch() -> Optional[str]:
    r = git("symbolic-ref", "-q", "--short", "HEAD")
    return r.out.strip() if r.rc == 0 else None


def upstream() -> Optional[str]:
    r = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    return r.out.strip() if r.rc == 0 else None


def ahead_behind() -> Tuple[int, int]:
    r = git("rev-list", "--left-right", "--count", "HEAD...@{u}")
    if r.rc != 0:
        return (0, 0)
    a, b = r.out.split()
    return int(a), int(b)


def in_progress() -> Optional[str]:
    gd = S.git_dir
    if os.path.isdir(os.path.join(gd, "rebase-merge")) or os.path.isdir(os.path.join(gd, "rebase-apply")):
        return "rebase"
    if os.path.exists(os.path.join(gd, "MERGE_HEAD")):
        return "merge"
    if os.path.exists(os.path.join(gd, "CHERRY_PICK_HEAD")):
        return "cherry-pick"
    if os.path.exists(os.path.join(gd, "REVERT_HEAD")):
        return "revert"
    return None


def bisecting() -> bool:
    return os.path.exists(os.path.join(S.git_dir, "BISECT_LOG"))


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def redact_url(url: str) -> str:
    return re.sub(r"(://[^/@:]+):[^@/]+@", r"\1:***@", url)


# ----------------------------------------------------------------------------
# Change model + scanning
# ----------------------------------------------------------------------------
UNMERGED = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


@dataclass
class Change:
    kind: str                        # created | updated | deleted | renamed | mode | submodule | group
    path: str
    old: Optional[str] = None
    staged: bool = False
    size: int = 0
    binary: bool = False
    reasons: List[str] = field(default_factory=list)   # why it looks like a secret
    notes: List[str] = field(default_factory=list)
    members: List["Change"] = field(default_factory=list)
    hook_retried: bool = False

    @property
    def paths(self) -> List[str]:
        if self.members:
            return [p for m in self.members for p in m.paths]
        return [self.old, self.path] if self.old else [self.path]

    @property
    def add_paths(self) -> List[str]:
        # a staged rename is already known to git; "git add old" would fail
        if self.members:
            return [p for m in self.members for p in m.add_paths]
        return [self.path]

    @property
    def shown(self) -> str:
        if self.members:
            return f"{self.path}/  ({len(self.members)} files)"
        return f"{self.old} -> {self.path}" if self.old else self.path

    def message(self, prefix: str) -> str:
        if self.kind == "renamed":
            body = f"renamed {self.old} -> {self.path}"
        elif self.kind == "mode":
            body = f"changed mode of {self.path}"
        elif self.kind == "submodule":
            body = f"updated submodule {self.path}"
        elif self.kind == "group":
            kinds = {m.kind for m in self.members}
            only = next(iter(kinds)) if len(kinds) == 1 else ""
            verb = only if only in ("created", "updated", "deleted") else "changed"
            where = "project root" if self.path == "." else f"{self.path}/"
            body = f"{verb} {len(self.members)} files in {where}"
        else:
            body = f"{self.kind} {self.path}"
        return prefix + body


def is_sensitive_name(path: str) -> bool:
    name = os.path.basename(path).lower()
    if name in SAFE_NAMES:
        return False
    return any(fnmatch.fnmatch(name, p) for p in SENSITIVE_NAMES)


def parse_status(untracked: bool = True) -> List[Tuple[str, str, Optional[str]]]:
    r = git("status", "--porcelain=v1", "-z", "-uall" if untracked else "-uno")
    if r.rc != 0:
        err("git status failed: " + first_line(r.err))
        sys.exit(1)
    parts = r.out.split("\0")
    out, i = [], 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        old = None
        if "R" in xy or "C" in xy:
            old = parts[i] if i < len(parts) else None
            i += 1
        out.append((xy, path, old))
    return out


def unmerged_entries() -> List[Tuple[str, str]]:
    return [(xy, p) for xy, p, _ in parse_status(False) if xy in UNMERGED]


def _numstat() -> Dict[str, Tuple[str, str]]:
    result: Dict[str, Tuple[str, str]] = {}
    if not head_sha():
        return result
    r = git("diff", "HEAD", "--numstat", "-z")
    if r.rc != 0:
        return result
    toks, i = r.out.split("\0"), 0
    while i < len(toks):
        tok = toks[i]
        parts = tok.split("\t")
        if len(parts) < 3:
            i += 1
            continue
        a, d, p = parts[0], parts[1], "\t".join(parts[2:])
        if p == "":                       # rename: next two tokens are old and new
            if i + 2 < len(toks):
                result[toks[i + 2]] = (a, d)
            i += 3
        else:
            result[p] = (a, d)
            i += 1
    return result


def _gitlinks() -> set:
    r = git("ls-files", "--stage", "-z")
    links = set()
    for ent in r.out.split("\0"):
        if ent.startswith("160000"):
            links.add(ent.split("\t", 1)[-1])
    return links


def scan_secrets(c: Change) -> List[str]:
    if c.kind in ("deleted", "submodule", "mode", "group") or c.binary:
        return []
    full = os.path.join(S.root, c.path)
    if os.path.islink(full) or not os.path.isfile(full):
        return []
    try:
        if os.path.getsize(full) > SCAN_MAX:
            return []
        if c.kind == "updated" and head_sha():
            r = git("diff", "HEAD", "-U0", "--no-color", "--", c.path)
            text = "\n".join(l[1:] for l in r.out.splitlines() if l.startswith("+") and not l.startswith("+++"))
        else:
            with open(full, "rb") as f:
                data = f.read()
            if b"\0" in data[:4096]:
                return []
            text = data.decode("utf-8", "ignore")
    except OSError:
        return []
    hits: List[str] = []
    for label, rx in SECRET_PATTERNS:
        if rx.search(text) and label not in hits:
            hits.append(label)
    return hits


TOOL_NAME_RX = re.compile(
    r"^(singulate|singulate[_-]pro|simple[_-]push|autocommit)(-(windows|macos|linux))?(\.py|\.exe)?$", re.I)


def frozen() -> bool:
    """True when running as a stand-alone executable built with PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def self_path() -> str:
    return os.path.realpath(sys.executable if frozen() else __file__)


def tool_cmd(script: str) -> str:
    """How the user should re-run us in a hint message."""
    return os.path.basename(sys.executable) if frozen() else f"python {script}"


def pause_if_double_clicked() -> None:
    """A Windows .exe opened by double-click closes instantly; keep the window open to read the result."""
    if not (frozen() and os.name == "nt"):
        return
    try:
        import ctypes
        procs = (ctypes.c_uint * 8)()
        # one-file builds have a bootloader + the real process; 2 or fewer means no shell started us
        if ctypes.windll.kernel32.GetConsoleProcessList(procs, 8) <= 2:
            input("\nPress Enter to close this window...")
    except Exception:
        pass


def run_as_script(main_fn) -> None:
    try:
        code = main_fn()
    except SystemExit as e:
        code = e.code
    pause_if_double_clicked()
    sys.exit(code)


def is_singulate_source_repo(root: str) -> bool:
    """True inside the Singulate project itself, where the scripts are meant to be committed."""
    try:
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as f:
            return bool(re.search(r'^name\s*=\s*"singulate"\s*$', f.read(), re.M))
    except OSError:
        return False


def own_tool_paths_in(root: str) -> set:
    """Repo-relative paths of this tool's own file(s) and its siblings, so we never commit ourselves."""
    if is_singulate_source_repo(root):
        return set()                        # this IS the Singulate repo: commit everything
    me = self_path()
    here = os.path.dirname(me)
    try:
        rel = os.path.relpath(here, os.path.realpath(root))
    except ValueError:                      # different drive on Windows
        return set()
    if rel == ".." or rel.startswith(".." + os.sep):
        return set()                        # the tool lives outside this repository
    try:
        names = {n for n in os.listdir(here) if TOOL_NAME_RX.match(n)}
    except OSError:
        names = set()
    names.add(os.path.basename(me))
    return {n if rel == "." else f"{rel}/{n}".replace(os.sep, "/") for n in names}


def own_tool_paths() -> set:
    return own_tool_paths_in(S.root)


def get_changes(args) -> Tuple[List[Change], List[str]]:
    entries = parse_status(True)
    gitlinks, numstat = _gitlinks(), _numstat()
    changes: List[Change] = []
    skipped: List[str] = []
    own = set() if args.include_self else own_tool_paths()
    for xy, path, old in entries:
        if xy in UNMERGED:
            continue
        if xy == "??" and path in own:       # never commit the Singulate tool itself
            skipped.append(f"{path}  (the Singulate tool itself, not part of your project; "
                           "use --include-self to commit it)")
            continue
        if path.endswith("/"):
            skipped.append(f"{path}  (a folder with its own .git - add it as a submodule yourself)")
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
        if path in gitlinks and kind != "deleted":
            c.kind = "submodule"
        elif kind == "updated" and numstat.get(path) == ("0", "0"):
            c.kind = "mode"
        if numstat.get(path, ("", ""))[0] == "-":
            c.binary = True
            c.notes.append("binary")
        if kind != "deleted":
            try:
                c.size = os.path.getsize(os.path.join(S.root, path))
            except OSError:
                c.size = 0
            if os.path.islink(os.path.join(S.root, path)):
                c.notes.append("symlink")
            if is_sensitive_name(path):
                c.reasons.append("secret-looking file name")
            if not args.no_content_scan:
                c.reasons.extend(scan_secrets(c))
        if kind == "renamed" and old and old.lower() == path.lower():
            c.notes.append("case-only rename")
        changes.append(c)
    return changes, skipped


# ----------------------------------------------------------------------------
# Review (preview, secrets, grouping, skip)
# ----------------------------------------------------------------------------
def size_text(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def show_list(changes: List[Change]) -> None:
    symbols = {"created": ("+", "32"), "updated": ("~", "33"), "deleted": ("-", "31"),
               "renamed": (">", "36"), "mode": ("%", "35"), "submodule": ("@", "35"), "group": ("#", "34")}
    print()
    for n, c in enumerate(changes, 1):
        sym, color = symbols[c.kind]
        label = {"mode": "chmod", "submodule": "submod"}.get(c.kind, c.kind)
        line = f"{n:>4}. {paint(sym, color)} {label:<7} {c.shown}"
        for note in c.notes:
            line += "  " + paint(f"({note})", "33")
        for rs in dict.fromkeys(c.reasons):
            line += "  " + paint(f"[{rs}]", "31")
        print(line)
    print()


def group_changes(changes: List[Change]) -> List[Change]:
    buckets: Dict[str, List[Change]] = {}
    for c in changes:
        top = c.path.split("/")[0] if "/" in c.path else "."
        buckets.setdefault(top, []).append(c)
    out: List[Change] = []
    for top, members in buckets.items():
        if len(members) == 1:
            out.append(members[0])
        else:
            g = Change(kind="group", path=top, members=members)
            if any(m.reasons for m in members):
                g.reasons = ["contains secret-looking files"]
            out.append(g)
    return out


def review(changes: List[Change], args) -> List[Change]:
    for c in changes:
        if c.staged:
            c.notes.append("already staged")
        if c.size >= LARGE_WARN:
            c.notes.append(f"large: {size_text(c.size)}")

    blocked = [c for c in changes if c.size >= LARGE_BLOCK]
    if blocked:
        warn("Skipping files over 100 MB (GitHub would reject them):")
        for c in blocked:
            print("   ", c.shown, f"({size_text(c.size)})")
        changes = [c for c in changes if c not in blocked]

    show_list(changes)
    if any(c.staged for c in changes):
        dim("Note: staged files still get their own commit; a file with staged and unstaged "
            "edits is committed whole.")

    risky = [c for c in changes if c.reasons]
    if risky:
        warn("These files may contain secrets. Pushing them to GitHub can leak credentials:")
        for c in risky:
            print("   ", c.shown, "-", ", ".join(dict.fromkeys(c.reasons)))
        if args.yes:
            choice = "e"
        else:
            choice = choose("[e] exclude them (default)  [i] include anyway  [a] abort: ", "eia", "e")
        if choice == "a":
            return []
        if choice == "e":
            changes = [c for c in changes if not c.reasons]
            ok(f"Excluded {len(risky)} file(s).")

    if len(changes) > args.group_threshold:
        warn(f"{len(changes)} files would become {len(changes)} separate commits.")
        if args.yes:
            choice = "g"
        else:
            choice = choose("[g] one commit per top-level folder  [c] continue per file  [a] abort: ", "gca", "g")
        if choice == "a":
            return []
        if choice == "g":
            changes = group_changes(changes)
            ok(f"Grouped into {len(changes)} commit(s).")

    if args.yes:
        return changes

    while changes:
        show_list(changes)
        ans = ask(f"Commit these {len(changes)} item(s), one commit each? "
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


# ----------------------------------------------------------------------------
# Identity, lock files, staging + commit with recovery
# ----------------------------------------------------------------------------
def have_identity() -> bool:
    return bool(git("config", "user.name").out.strip()) and bool(git("config", "user.email").out.strip())


def ensure_identity(args) -> bool:
    if have_identity():
        return True
    warn("Git doesn't know who you are (user.name / user.email), so it can't commit.")
    if args.yes:
        err("Run: git config user.name \"Your Name\" && git config user.email you@example.com")
        return False
    name = ask("Your name: ")
    email = ask("Your email (the one on your GitHub account): ")
    if not name or "@" not in email:
        err("Need a name and a valid email.")
        return False
    git("config", "--local", "user.name", name)
    git("config", "--local", "user.email", email)
    ok("Saved for this repository only.")
    return True


def handle_lock(args) -> bool:
    lock = os.path.join(S.git_dir, "index.lock")
    if not os.path.exists(lock):
        return False
    age = time.time() - os.path.getmtime(lock)
    if age < 3:                       # probably another git command finishing right now
        time.sleep(3)
        if not os.path.exists(lock):
            return True
    if args.yes:
        err(f"{lock} exists. Make sure no other git program is running, then delete it.")
        return False
    warn(f"Found a leftover lock file ({int(age)}s old): {lock}")
    dim("It's safe to remove only if no other git operation (IDE, terminal) is running.")
    if yes_no("Remove it and retry?", default=False):
        try:
            os.remove(lock)
            return True
        except OSError as e:
            err(f"Couldn't remove it: {e}")
    return False


def recover(text: str, c: Change, args) -> bool:
    """Try to fix a known failure. Returns True if the operation should be retried."""
    low = text.lower()
    if "index.lock" in low:
        return handle_lock(args)
    if any(k in low for k in ("author identity unknown", "please tell me who you are",
                              "unable to auto-detect email", "empty ident name")):
        return ensure_identity(args)
    if "gpg" in low and ("failed to sign" in low or "failed to write commit object" in low):
        if S.gpg_off or args.yes:
            return False
        warn("Commit signing (GPG) failed:\n" + clip(text, 2))
        if yes_no("Commit WITHOUT signing for the rest of this run?", default=False):
            S.gpg_off = True
            return True
        return False
    if not c.hook_retried and c.members == []:
        st = git("status", "--porcelain", "-z", "--", c.path).out
        if len(st) >= 2 and st[1] not in (" ", "?") and st[0] != "?":
            c.hook_retried = True       # a hook changed the file after staging: re-stage once
            return True
    return False


def _with_paths(prefix: List[str], paths: List[str]) -> R:
    if len(paths) > 40 and git_version() >= (2, 25, 0):
        return git(*prefix, "--pathspec-from-file=-", "--pathspec-file-nul", input="\0".join(paths) + "\0")
    return git(*prefix, "--", *paths)


def has_commit_hooks() -> bool:
    d = git("rev-parse", "--git-path", "hooks").out.strip()
    d = d if os.path.isabs(d) else os.path.join(S.root, d)
    return any(os.path.isfile(os.path.join(d, h)) for h in ("pre-commit", "commit-msg", "prepare-commit-msg"))


def stage_and_commit(c: Change, msg: str, args) -> Tuple[str, str]:
    for _ in range(4):
        r = _with_paths(["add"], c.add_paths)
        if r.rc != 0:
            if recover(r.text, c, args):
                continue
            return "fail", first_line(r.err)
        if "will be replaced by" in r.err and "line-ending" not in " ".join(S.notes):
            S.notes.append("line-ending conversion warnings (check your .gitattributes / core.autocrlf)")
        prefix = (["-c", "commit.gpgsign=false"] if S.gpg_off else []) + ["commit", "-m", msg]
        r = _with_paths(prefix, c.paths)
        if r.rc == 0:
            return "ok", ""
        text = r.text
        if "nothing to commit" in text or "no changes added" in text or "nothing added to commit" in text:
            return "noop", "no effective change"
        if recover(text, c, args):
            continue
        hint = first_line(r.err or r.out)
        if "hook" in text.lower() or has_commit_hooks():
            hint = "a git hook rejected the commit:\n" + clip(text, 3)
        return "fail", hint
    return "fail", "gave up after several retries"


# ----------------------------------------------------------------------------
# Run record (undo + interrupted-run memory)
# ----------------------------------------------------------------------------
def state_path() -> str:
    return os.path.join(S.git_dir, "singulate-state.json")


def load_state() -> Optional[dict]:
    try:
        with open(state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(st: dict) -> None:
    try:
        with open(state_path(), "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1)
    except OSError:
        pass


def start_record(args) -> dict:
    """Begin (or continue) the run record and create a backup ref of the starting commit."""
    prev = load_state()
    head = head_sha()
    if prev and not prev.get("finished") and not prev.get("pushed") and prev.get("commits") \
            and prev["commits"][-1]["sha"] == head:
        dim("Continuing an interrupted earlier run (undo will cover both).")
        prev["finished"] = False
        return prev
    st = {"version": 1, "start": head, "branch": current_branch(), "backup_ref": None,
          "started": datetime.now(timezone.utc).isoformat(), "commits": [], "pushed": False, "finished": False}
    if head and not args.dry_run:
        ref = f"refs/singulate/backup-{utc_stamp()}"
        if git("update-ref", ref, head).rc == 0:
            st["backup_ref"] = ref
            refs = sorted(git("for-each-ref", "--format=%(refname)", "refs/singulate/").out.split())
            for old in refs[:-5]:
                git("update-ref", "-d", old)
    return st


def run_undo(args) -> int:
    st = load_state()
    if not st or not st.get("commits"):
        print("Nothing to undo: no earlier run recorded in this repository.")
        return 1
    n = len(st["commits"])
    if st.get("pushed"):
        err(f"The last run's {n} commit(s) were already pushed.")
        print("Undoing them would need a force-push, which Singulate never does.")
        print("To reverse a single change safely use:  git revert <commit>")
        return 1
    if head_sha() != st["commits"][-1]["sha"] or current_branch() != st.get("branch"):
        err("The repository has moved on since that run (different branch or newer commits).")
        print("Refusing to undo automatically. Your backup ref, if you need it:", st.get("backup_ref") or "(none)")
        return 1
    print(f"Undo the last run: {n} commit(s) on '{st.get('branch')}'.")
    print("Your files stay exactly as they are, but become uncommitted changes again.")
    if args.dry_run:
        dim("[dry-run] nothing changed")
        return 0
    if not (args.yes or yes_no("Proceed?", default=False)):
        return 0
    start = st.get("start")
    if start:
        r = git("reset", "-q", start)
    else:                               # the run made the very first commits of a new repo
        r = git("update-ref", "-d", "HEAD")
        if r.rc == 0:
            subprocess.run(["git", "rm", "-r", "-q", "--cached", "."], cwd=S.root,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    if r.rc != 0:
        err("Undo failed: " + first_line(r.err))
        return 1
    st["commits"], st["finished"] = [], True
    save_state(st)
    ok("Undone. Run `git status` to see your changes again.")
    return 0


# ----------------------------------------------------------------------------
# Operations in progress + guided conflict resolution
# ----------------------------------------------------------------------------
OPS_CONT = {"merge": ["merge", "--continue"], "rebase": ["rebase", "--continue"],
            "cherry-pick": ["cherry-pick", "--continue"], "revert": ["revert", "--continue"]}
OPS_ABORT = {"merge": ["merge", "--abort"], "rebase": ["rebase", "--abort"],
             "cherry-pick": ["cherry-pick", "--abort"], "revert": ["revert", "--abort"]}
CONFLICT_DESC = {"UU": "both sides modified it", "AA": "both sides added it",
                 "DU": "you deleted it, they modified it", "UD": "they deleted it, you modified it",
                 "AU": "added on your side only", "UA": "added on their side only", "DD": "both sides deleted it"}
NO_EDITOR_ENV = {"GIT_EDITOR": "true"}


def side_names(op: Optional[str], stash_mode: bool) -> Dict[str, str]:
    """Map the human words 'yours' / 'theirs' to git's --ours / --theirs (they flip during a rebase)."""
    if op == "rebase" or stash_mode:
        return {"yours": "--theirs", "theirs": "--ours"}
    return {"yours": "--ours", "theirs": "--theirs"}


def explain_sides(op: Optional[str], stash_mode: bool) -> None:
    if stash_mode:
        msg = "yours = your uncommitted edits, theirs = the changes that were just pulled in"
    elif op == "rebase":
        msg = "yours = your commit being replayed, theirs = what is already on the upstream branch"
    elif op in ("cherry-pick", "revert"):
        msg = f"yours = your current branch, theirs = the commit being {op.replace('-', ' ')}ed"
    else:
        msg = "yours = your current branch, theirs = the branch being merged in"
    dim(f"({msg})")


def stages_of(path: str) -> set:
    r = git("ls-files", "-u", "--", path)
    out = set()
    for line in r.out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2].isdigit():
            out.add(int(parts[2]))
    return out


def has_markers(path: str) -> bool:
    try:
        with open(os.path.join(S.root, path), "r", encoding="utf-8", errors="ignore") as f:
            t = f.read()
    except OSError:
        return False
    return bool(re.search(r"^<<<<<<<(?: |$)", t, re.M) and re.search(r"^>>>>>>>(?: |$)", t, re.M))


def marker_count(path: str) -> int:
    try:
        with open(os.path.join(S.root, path), "r", encoding="utf-8", errors="ignore") as f:
            return len(re.findall(r"^<<<<<<<(?: |$)", f.read(), re.M))
    except OSError:
        return 0


def is_binary_file(path: str) -> bool:
    try:
        with open(os.path.join(S.root, path), "rb") as f:
            return b"\0" in f.read(8000)
    except OSError:
        return False


def get_editor() -> str:
    for v in (os.environ.get("GIT_EDITOR"), git("config", "core.editor").out.strip(),
              os.environ.get("VISUAL"), os.environ.get("EDITOR")):
        if v:
            return v
    if os.environ.get("TERM_PROGRAM") == "vscode" and shutil.which("code"):
        return "code --wait"
    return "notepad" if os.name == "nt" else ("nano" if shutil.which("nano") else "vi")


def open_editor(path: str) -> bool:
    cmd = shlex.split(get_editor(), posix=(os.name != "nt")) + [os.path.join(S.root, path)]
    try:
        return subprocess.run(cmd, cwd=S.root).returncode == 0
    except OSError as e:
        err(f"Couldn't start the editor: {e}")
        return False


def take_side(path: str, flag: str) -> bool:
    """Resolve a conflicted path using one side. If that side deleted the file, accept the deletion."""
    stage = 2 if flag == "--ours" else 3
    if stage not in stages_of(path):
        r = git("rm", "-q", "-f", "--", path)
        return r.rc == 0
    r = git("checkout", flag, "--", path)
    if r.rc != 0:
        err("checkout failed: " + first_line(r.err))
        return False
    return git("add", "--", path).rc == 0


def union_merge(path: str) -> bool:
    import tempfile
    rc2, ours = git_bytes("show", f":2:{path}")
    rc3, theirs = git_bytes("show", f":3:{path}")
    rc1, base = git_bytes("show", f":1:{path}")
    if rc2 != 0 or rc3 != 0:
        return False
    if rc1 != 0:
        base = b""
    with tempfile.TemporaryDirectory() as td:
        paths = []
        for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = os.path.join(td, name)
            with open(p, "wb") as f:
                f.write(data)
            paths.append(p)
        p = subprocess.run(["git", "merge-file", "-p", "--union", *paths], capture_output=True,
                           stdin=subprocess.DEVNULL)
    if p.returncode >= 128:
        return False
    with open(os.path.join(S.root, path), "wb") as f:
        f.write(p.stdout)
    return git("add", "--", path).rc == 0


def stage_resolved(path: str) -> bool:
    """The user says it's fixed: add it (or record the deletion)."""
    if os.path.exists(os.path.join(S.root, path)):
        return git("add", "--", path).rc == 0
    return git("rm", "-q", "-f", "--", path).rc == 0


def resolve_file(path: str, xy: str, sides: Dict[str, str], args) -> str:
    """Returns 'resolved' | 'skipped' | 'abort'."""
    present = stages_of(path)
    both = 2 in present and 3 in present
    print()
    print(paint(f"Conflict: {path}", "1") + paint(f"  - {CONFLICT_DESC.get(xy, xy)}", "33"))
    yours_stage = 2 if sides["yours"] == "--ours" else 3

    if not both:
        if present - {1}:
            have = (present - {1}).pop()
            who = "YOUR side" if have == yours_stage else "THEIR side"
            print(f"  Only {who} still has this file; the other side deleted it.")
            flag = "--ours" if have == 2 else "--theirs"
            menu, valid = "  [k] keep the file  [r] remove it  [s] skip  [a] abort everything: ", "krsa"
        else:
            print("  Both sides deleted it.")
            flag, menu, valid = None, "  [r] remove it  [s] skip  [a] abort everything: ", "rsa"
        while True:
            c = choose(menu, valid)
            if c == "k":
                if take_side(path, flag):
                    break
            elif c == "r":
                if git("rm", "-q", "-f", "--", path).rc == 0:
                    break
                err("Couldn't remove it.")
            elif c == "s":
                return "skipped"
            elif c == "a":
                if yes_no("Abort the whole operation and go back to before it started?", False):
                    return "abort"
        S.conflicts_resolved += 1
        ok(f"✔ resolved {path}")
        return "resolved"

    binary = is_binary_file(path)
    if binary:
        print("  Binary file - it can't be merged line by line.")
        menu, valid = "  [y] keep yours  [t] take theirs  [s] skip  [a] abort everything: ", "ytsa"
    else:
        n = marker_count(path)
        print(f"  {n} conflicting section(s)." if n else "  (no markers found in the working copy)")
        menu = ("  [y] keep yours  [t] take theirs  [b] keep both  [e] edit  [r] I fixed it myself  "
                "[d] diff  [s] skip  [a] abort everything: ")
        valid = "ytberdsa"

    while True:
        c = choose(menu, valid)
        done = False
        if c == "y":
            done = take_side(path, sides["yours"])
        elif c == "t":
            done = take_side(path, sides["theirs"])
        elif c == "b":
            done = union_merge(path)
            if not done:
                err("Couldn't combine both sides automatically.")
        elif c == "e":
            if open_editor(path):
                if has_markers(path):
                    warn("Conflict markers (<<<<<<< / >>>>>>>) are still in the file.")
                else:
                    done = stage_resolved(path)
        elif c == "r":
            if has_markers(path):
                warn("Conflict markers (<<<<<<< / >>>>>>>) are still in the file.")
            else:
                done = stage_resolved(path)
        elif c == "d":
            git_live("--no-pager", "diff", "--", path)
        elif c == "s":
            return "skipped"
        elif c == "a":
            if yes_no("Abort the whole operation and go back to before it started?", False):
                return "abort"
        if done:
            S.conflicts_resolved += 1
            ok(f"✔ resolved {path}")
            return "resolved"


def abort_operation(op: Optional[str], stash_mode: bool, paths: List[str]) -> None:
    if op:
        r = git(*OPS_ABORT[op])
        if r.rc == 0:
            ok(f"The {op} was aborted. Everything is back to how it was before it started.")
        else:
            err(f"Couldn't abort the {op}: " + first_line(r.err))
    elif stash_mode and paths:
        git("reset", "-q", "--", *paths)
        git("checkout", "--", *paths)
        ok("Conflicted files were reset. Your own edits are safe in the stash "
           "(see `git stash list`; restore later with `git stash pop`).")


def finish_operation(op: str) -> str:
    """Continue a merge/rebase/cherry-pick/revert once no conflicts remain. -> done | again | error"""
    r = git(*OPS_CONT[op], env=NO_EDITOR_ENV)
    if r.rc == 0:
        return "again" if in_progress() else "done"
    if unmerged_entries():
        return "again"
    low = r.text.lower()
    if "no changes" in low or "nothing to commit" in low or "now empty" in low or "empty" in low:
        skip = ["rebase", "--skip"] if op == "rebase" else [op, "--skip"]
        r2 = git(*skip, env=NO_EDITOR_ENV)
        if r2.rc == 0:
            return "again" if in_progress() else "done"
        if unmerged_entries():
            return "again"
    err(f"Couldn't continue the {op}:\n" + clip(r.text, 4))
    return "error"


def finish_stash_conflict(paths: List[str]) -> None:
    git("reset", "-q", "--", *paths)          # un-stage, keep the resolved content as plain edits
    top = git("stash", "list", "-1", "--format=%gs").out
    if "autostash" in top:
        git("stash", "drop", "-q")
    ok("Your local edits are back as normal uncommitted changes.")


def conflict_session(args) -> str:
    """Guide the user through conflicts. Returns 'resolved' | 'aborted' | 'pending'."""
    unm = unmerged_entries()
    stash_mode = in_progress() is None and bool(unm)
    resolved_paths: List[str] = []
    while True:
        op = in_progress()
        unm = unmerged_entries()
        if unm:
            if args.yes:
                warn("Conflicts need a human decision; --yes never resolves them.")
                abort_operation(op, stash_mode, [p for _, p in unm])
                return "aborted"
            sides = side_names(op, stash_mode)
            bold(f"\n{len(unm)} conflicted file(s) to resolve")
            explain_sides(op, stash_mode)
            skipped: List[str] = []
            for xy, path in unm:
                res = resolve_file(path, xy, sides, args)
                if res == "abort":
                    abort_operation(op, stash_mode, [p for _, p in unm])
                    return "aborted"
                if res == "skipped":
                    skipped.append(path)
                else:
                    resolved_paths.append(path)
            if skipped:
                warn(f"{len(skipped)} file(s) are still unresolved.")
                a = choose("[a] abort the whole operation  [l] leave it as it is and stop (finish by hand): ", "al", "l")
                if a == "a":
                    abort_operation(op, stash_mode, skipped)
                    return "aborted"
                print("Left in progress. Finish by hand, then run this tool again.")
                return "pending"
            continue
        if op:
            res = finish_operation(op)
            if res == "again":
                continue
            if res == "done":
                ok(f"The {op} finished.")
                return "resolved"
            return "pending"
        if stash_mode and resolved_paths:
            finish_stash_conflict(resolved_paths)
        return "resolved"


def startup_operation(args) -> bool:
    """Deal with a merge/rebase/... already in progress. True = safe to continue the normal run."""
    op, unm = in_progress(), unmerged_entries()
    if not op and not unm:
        if bisecting():
            dim("Note: a git bisect session is active; commits made now are part of that session.")
        return True
    if op:
        warn(f"A {op} is already in progress" + (f" with {len(unm)} conflicted file(s)." if unm else "."))
    else:
        warn(f"{len(unm)} conflicted file(s) are left over (probably from a stash).")
    if args.yes:
        err("Finish or abort it first, then run again.")
        return False
    if args.dry_run:
        dim("[dry-run] would offer to resolve / continue / abort")
        return False
    c = choose("[r] resolve & continue it now  [a] abort it  [q] quit: ", "raq", "r")
    if c == "q":
        return False
    if c == "a":
        abort_operation(op, op is None, [p for _, p in unm])
        return in_progress() is None
    return conflict_session(args) == "resolved"


# ----------------------------------------------------------------------------
# Pull, detached HEAD
# ----------------------------------------------------------------------------
def pull_step(args) -> bool:
    if args.pull:
        do = True
    elif args.no_pull or args.yes:
        do = False
    else:
        do = yes_no("Pull first?", default=False)
    if not do:
        return True
    up = upstream()
    if not up:
        dim("No upstream branch is configured, so there is nothing to pull.")
        return True
    if args.dry_run:
        dim("[dry-run] would fetch and pull")
        return True
    if S.push_blocked and not S.login_deferred:
        warn("Skipping the pull: you're not logged in.")
        return True
    print("Fetching...")
    if git_live("fetch", "--quiet") != 0:
        warn("Fetch failed (see above).")
        if args.yes or not yes_no("Continue without pulling?", default=False):
            return False
        return True
    ahead, behind = ahead_behind()
    if behind == 0:
        ok("Already up to date.")
        return True
    if ahead == 0:
        cmd = ["pull", "--ff-only", "--autostash"]
    else:
        warn(f"Your branch and {up} have diverged: you are {ahead} ahead and {behind} behind.")
        if args.yes:
            err("Pick merge or rebase yourself (run without -y).")
            return False
        c = choose("[m] merge  [r] rebase  [s] stop here: ", "mrs", "s")
        if c == "s":
            return False
        cmd = ["pull", "--no-rebase", "--no-edit", "--autostash"] if c == "m" else ["pull", "--rebase", "--autostash"]
    rc = git_live(*cmd)
    if unmerged_entries() or in_progress():
        return conflict_session(args) == "resolved"
    if rc != 0:
        err("Pull failed (see above).")
        if args.yes or not yes_no("Continue without pulling?", default=False):
            return False
    return True


def ensure_branch(args) -> bool:
    if current_branch() is not None:
        return True
    warn("You're on a detached HEAD: commits made now are easy to lose when you switch away.")
    if args.yes:
        err("Create a branch first:  git switch -c my-branch")
        return False
    c = choose("[b] create a branch here  [c] continue detached  [a] abort: ", "bca", "b")
    if c == "a":
        return False
    if c == "b":
        while True:
            name = ask("New branch name: ")
            if name and git("check-ref-format", "--branch", name).rc == 0:
                break
            print("That isn't a valid branch name.")
        r = git("switch", "-c", name)
        if r.rc != 0:
            err("Couldn't create the branch: " + first_line(r.err))
            return False
        ok(f"Switched to new branch '{name}'.")
    return True


# ----------------------------------------------------------------------------
# Remotes + login detection
# ----------------------------------------------------------------------------
@dataclass
class Remote:
    name: str
    url: str
    scheme: str                      # https | http | ssh | local | other
    host: str = ""
    owner: str = ""
    repo: str = ""
    user: Optional[str] = None
    secret: Optional[str] = None     # password/token embedded in the URL (bad!)
    port: Optional[str] = None


@dataclass
class Login:
    status: str                      # ok | no_write | no_auth | offline | unknown
    handle: Optional[str] = None
    method: str = ""                 # gh | ssh | credential | url | local
    detail: str = ""


def parse_remote(name: str, url: str) -> Remote:
    m = re.match(r"^(?P<scheme>https?|ssh|git)://(?:(?P<ui>[^@/]+)@)?(?P<host>[^/:]+)(?::(?P<port>\d+))?/(?P<path>.+)$", url)
    user = secret = port = None
    if m:
        scheme, host, path, port = m["scheme"], m["host"], m["path"], m["port"]
        ui = m["ui"]
        if ui:
            user, _, sec = ui.partition(":")
            secret = sec or None
        if scheme == "git":
            scheme = "other"
    else:
        m2 = re.match(r"^(?:(?P<user>[^@/\\]+)@)?(?P<host>[^:/\\]{2,}):(?P<path>(?!//).+)$", url)
        if m2 and not re.match(r"^[A-Za-z]:[\\/]", url):
            scheme, host, path, user = "ssh", m2["host"], m2["path"], m2["user"]
        else:
            return Remote(name, url, "local")
    path = re.sub(r"\.git/?$", "", path.rstrip("/"))
    segs = path.split("/")
    owner = segs[-2] if len(segs) >= 2 else ""
    return Remote(name, url, scheme, host, owner, segs[-1], user, secret, port)


def pick_remote() -> Optional[Remote]:
    b = current_branch()
    name = git("config", f"branch.{b}.remote").out.strip() if b else ""
    names = git("remote").out.split()
    if not name or name not in names:
        name = "origin" if "origin" in names else (names[0] if names else "")
    if not name:
        return None
    url = git("remote", "get-url", "--push", name).out.strip()
    return parse_remote(name, url) if url else None


def run_cmd(cmd: List[str], timeout: int = 25, env: Optional[Dict[str, str]] = None) -> R:
    e = os.environ.copy()
    e["GH_PROMPT_DISABLED"] = "1"
    if env:
        e.update(env)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout, env=e)
    except FileNotFoundError:
        return R(127, "", "not found")
    except subprocess.TimeoutExpired:
        return R(124, "", "timeout")
    return R(p.returncode, p.stdout, p.stderr)


def parse_ssh_greeting(text: str) -> Optional[str]:
    for rx in (r"Hi there, ([\w.-]+)!", r"Hi ([\w.-]+)!", r"Welcome to GitLab, @([\w.-]+)!",
               r"logged in as ([\w.-]+)\."):
        m = re.search(rx, text)
        if m:
            return m.group(1)
    return None


def ssh_identity(host: str, user: str = "git", key: Optional[str] = None,
                 port: Optional[str] = None) -> Tuple[Optional[str], str]:
    """Ask the host who our SSH key is. -> (handle, outcome) outcome: ok|denied|offline|hostkey|unknown"""
    cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if key:
        cmd += ["-i", key, "-o", "IdentitiesOnly=yes"]
    if port:
        cmd += ["-p", port]
    cmd.append(f"{user or 'git'}@{host}")
    r = run_cmd(cmd, timeout=25)
    text, low = r.text, r.text.lower()
    handle = parse_ssh_greeting(text)
    if handle or "successfully authenticated" in low:
        return handle, "ok"
    if r.rc == 127:
        return None, "unknown"
    if "permission denied" in low:
        return None, "denied"
    if "host key verification failed" in low:
        return None, "hostkey"
    if any(k in low for k in ("could not resolve", "timed out", "network is unreachable",
                              "connection refused", "no route to host")) or r.rc == 124:
        return None, "offline"
    return None, "unknown"


def gh_login(host: str) -> Optional[str]:
    if not shutil.which("gh"):
        return None
    r = run_cmd(["gh", "api", "--hostname", host, "--jq", ".login", "user"], timeout=15)
    return r.out.strip() or None if r.rc == 0 else None


def gh_accounts(host: str) -> List[str]:
    if not shutil.which("gh"):
        return []
    r = run_cmd(["gh", "auth", "status", "--hostname", host], timeout=15)
    return list(dict.fromkeys(re.findall(r"Logged in to \S+ (?:account|as) (\S+)", r.text)))


def https_identity(rm: Remote) -> Tuple[Optional[str], str]:
    if rm.user:
        return rm.user, "url"
    helpers = git("config", "--get-regexp", r"^credential(\..*)?\.helper$").out
    if re.search(r"\bgh\b.*git-credential|gh auth git-credential", helpers):
        h = gh_login(rm.host)
        if h:
            return h, "gh"
    r = git("credential", "fill", input=f"protocol={rm.scheme}\nhost={rm.host}\n\n",
            noninteractive=True, timeout=15)
    if r.rc == 0:
        m = re.search(r"^username=(.+)$", r.out, re.M)
        if m:
            return m.group(1).strip(), "credential"     # the password in r.out is never kept or shown
    return None, ""


def classify(r: R) -> Tuple[str, Optional[str], str]:
    """Turn a git network command's result into (status, handle-if-known, detail)."""
    if r.rc == 0:
        return "ok", None, ""
    t, low = r.text, r.text.lower()
    m = re.search(r"Permission to \S+ denied to ([A-Za-z0-9_-]+)", t)
    if m:
        return "no_write", m.group(1), f"{m.group(1)} has no write access to this repository"
    if r.rc == 124:
        return "unknown", None, "the check timed out"
    if any(k in low for k in ("could not resolve host", "could not resolve hostname", "network is unreachable",
                              "connection timed out", "connection refused", "failed to connect",
                              "no route to host", "operation timed out")):
        return "offline", None, first_line(r.err)
    if "host key verification failed" in low:
        return "unknown", None, "SSH host key not trusted yet - run `ssh -T git@<host>` once and accept it"
    if any(k in low for k in ("authentication failed", "could not read username", "could not read password",
                              "permission denied (publickey", "terminal prompts disabled",
                              "invalid username or password", "returned error: 401", "returned error: 403",
                              "bad credentials", "repository not found", "invalid username", "access denied")):
        return "no_auth", None, first_line(r.err)
    return "unknown", None, first_line(r.err)


def probe_push_access(rm: Remote, extra: Optional[List[str]] = None,
                      env: Optional[Dict[str, str]] = None) -> Tuple[str, Optional[str], str]:
    """Can we authenticate for writing? Uses a dry-run push to a throwaway branch name (changes nothing)."""
    if rm.scheme == "local":
        return "ok", None, "local remote"
    pre = extra or []
    if head_sha():
        r = git(*pre, "push", "--dry-run", "--porcelain", rm.name, "HEAD:refs/heads/singulate-login-check",
                noninteractive=True, timeout=30, env=env)
    else:
        r = git(*pre, "ls-remote", "--heads", rm.name, noninteractive=True, timeout=30, env=env)
    return classify(r)


def detect_login(rm: Remote) -> Login:
    if rm.scheme == "local":
        return Login("ok", None, "local", "local remote - no login needed")
    handle, method = None, ""
    if rm.scheme == "ssh":
        handle, outcome = ssh_identity(rm.host, rm.user or "git", port=rm.port)
        method = "ssh"
        if outcome == "denied":
            return Login("no_auth", None, "ssh", "no SSH key was accepted")
        if outcome == "offline":
            return Login("offline", None, "ssh", "can't reach the host")
    elif rm.scheme in ("https", "http"):
        handle, method = https_identity(rm)
    else:
        return Login("unknown", None, "", f"login check isn't supported for {rm.scheme}:// remotes")
    state, who, detail = probe_push_access(rm)
    return Login(state, who or handle, method, detail)


def show_login(lg: Login, rm: Remote) -> None:
    how = {"gh": "GitHub CLI", "ssh": "SSH key", "credential": "saved credentials", "url": "login in URL"}.get(lg.method, lg.method)
    who = f"@{lg.handle}" if lg.handle else "(account name unavailable)"
    if lg.status == "ok":
        S.login_line = f"{rm.host} as {who} via {how}" if rm.scheme != "local" else "local remote"
        if rm.scheme != "local":
            ok(f"✔ Logged in to {rm.host} as {who}  ({how})")
    elif lg.status == "no_write":
        S.login_line = f"{rm.host} as {who} (no write access)"
        warn(f"⚠ Logged in to {rm.host} as {who}, but that account can't push to {rm.owner}/{rm.repo}.")
    elif lg.status == "no_auth":
        S.login_line = f"not logged in to {rm.host}"
        warn(f"✘ Not logged in to {rm.host}" + (f"  ({lg.detail})" if lg.detail else ""))
        if lg.handle:
            dim(f"  The saved login for @{lg.handle} was rejected (expired or revoked token?).")
    elif lg.status == "offline":
        S.login_line = f"{rm.host} unreachable"
        warn(f"⚠ Can't reach {rm.host} (offline?). Commits will work; pushing needs a connection.")
    else:
        S.login_line = f"{rm.host}: unverified"
        warn(f"⚠ Couldn't verify the login for {rm.host}: {lg.detail or 'unknown reason'}")
        dim("  Git may still ask for your credentials when you push.")


# ----------------------------------------------------------------------------
# Login flows (GitHub CLI / token / SSH) and account switching
# ----------------------------------------------------------------------------
def convert_url(rm: Remote, to: str) -> str:
    if to == "https":
        return f"https://{rm.host}/{rm.owner}/{rm.repo}.git"
    return f"git@{rm.host}:{rm.owner}/{rm.repo}.git"


def offer_convert(rm: Remote, to: str, why: str) -> bool:
    new = convert_url(rm, to)
    print(f"  {why}, but this remote uses {rm.scheme.upper()}.")
    print(f"  Switch '{rm.name}' from  {redact_url(rm.url)}  to  {new} ?")
    if not yes_no("  Change the remote URL?", default=True):
        return False
    r = git("remote", "set-url", rm.name, new)
    if r.rc != 0:
        err("Couldn't change the URL: " + first_line(r.err))
        return False
    ok("Remote URL updated.")
    return True


def flow_gh(rm: Remote) -> bool:
    if "github" not in rm.host:
        print("  The GitHub CLI only works with GitHub hosts. Use the token or SSH option instead.")
        return False
    if not shutil.which("gh"):
        print("  The GitHub CLI (gh) isn't installed. Get it from https://cli.github.com/ and choose this again.")
        return False
    if rm.scheme == "ssh" and not offer_convert(rm, "https", "The GitHub CLI signs in over HTTPS"):
        return False
    rc = subprocess.run(["gh", "auth", "login", "--hostname", rm.host, "--web", "--git-protocol", "https"]).returncode
    if rc != 0:
        err("GitHub CLI login didn't complete.")
        return False
    subprocess.run(["gh", "auth", "setup-git", "--hostname", rm.host], stdout=subprocess.DEVNULL)
    return True


def default_helper() -> str:
    if os.name == "nt":
        return "manager"
    if platform.system() == "Darwin":
        return "osxkeychain"
    return "cache --timeout=7200"


def store_credential(rm: Remote, user: str, token: str, old_handle: Optional[str] = None) -> bool:
    if not git("config", "--get-all", "credential.helper").out.strip():
        h = default_helper()
        print(f"  Git has no credential helper, so it would forget the token right after this run.")
        if yes_no(f"  Enable the '{h}' helper for your user account (git config --global)?", default=True):
            git("config", "--global", "credential.helper", h)
        else:
            warn("  Token not saved - git will ask again next time.")
            return False
    if old_handle and old_handle != user and yes_no(f"  Forget the saved login for @{old_handle} on {rm.host}?", True):
        git("credential", "reject", input=f"protocol={rm.scheme}\nhost={rm.host}\nusername={old_handle}\n\n")
    r = git("credential", "approve",
            input=f"protocol={rm.scheme}\nhost={rm.host}\nusername={user}\npassword={token}\n\n")
    return r.rc == 0


def flow_token(rm: Remote, old_handle: Optional[str] = None) -> bool:
    if rm.scheme == "ssh" and not offer_convert(rm, "https", "Tokens work over HTTPS"):
        return False
    rm = pick_remote() or rm
    if "github" in rm.host:
        print("  Create a token at https://github.com/settings/tokens")
        print("  (classic: tick 'repo'  |  fine-grained: Contents = Read and write).")
    user = ask(f"  Your {rm.host} username: ")
    token = ask_secret("  Access token (typing is hidden): ")
    if not user or not token:
        return False
    helper = '!f() { echo "username=$SING_U"; echo "password=$SING_P"; }; f'
    state, who, detail = probe_push_access(
        rm, extra=["-c", "credential.helper=", "-c", f"credential.helper={helper}"],
        env={"SING_U": user, "SING_P": token})
    if state != "ok":
        err("  That token didn't work: " + (detail or state))
        return False
    saved = store_credential(rm, user, token, old_handle)
    del token
    return saved


def ssh_keys() -> List[str]:
    import glob
    d = os.path.expanduser("~/.ssh")
    return [p[:-4] for p in sorted(glob.glob(os.path.join(d, "*.pub"))) if os.path.isfile(p[:-4])]


def use_ssh_key(key: str) -> None:
    S.env["GIT_SSH_COMMAND"] = f"ssh -i {shlex.quote(key)} -o IdentitiesOnly=yes"
    if yes_no("  Remember this key for this repository (git config core.sshCommand)?", default=True):
        git("config", "--local", "core.sshCommand", S.env["GIT_SSH_COMMAND"])


def flow_ssh(rm: Remote, switching: bool = False) -> bool:
    if rm.scheme != "ssh":
        if not offer_convert(rm, "ssh", "SSH keys need an SSH URL"):
            return False
        rm = pick_remote() or rm
    keys = ssh_keys()
    results = []
    if keys:
        print(f"  Testing {len(keys)} SSH key(s) against {rm.host}...")
        for k in keys:
            h, o = ssh_identity(rm.host, rm.user or "git", key=k, port=rm.port)
            results.append((k, h, o))
    working = [(k, h) for k, h, o in results if o == "ok"]
    if working:
        for i, (k, h) in enumerate(working, 1):
            print(f"   {i}. {os.path.basename(k)}  ->  {('@' + h) if h else 'accepted'}")
        if len(working) == 1 and not switching:
            use_ssh_key(working[0][0])
            return True
        pick = choose(f"  Use which key? [1-{len(working)}]: ", "".join(str(i) for i in range(1, min(len(working), 9) + 1)))
        use_ssh_key(working[int(pick) - 1][0])
        return True
    # nothing works yet: pick or create a key and show the public part
    if keys:
        print("  None of your keys is registered with this host.")
        key = keys[0] if len(keys) == 1 else keys[int(choose(
            "  Use which one? " + ", ".join(f"{i}={os.path.basename(k)}" for i, k in enumerate(keys, 1)) + ": ",
            "".join(str(i) for i in range(1, min(len(keys), 9) + 1))) ) - 1]
    else:
        if not yes_no("  You have no SSH key yet. Create one now?", default=True):
            return False
        email = git("config", "user.email").out.strip() or ask("  Email for the key label: ")
        os.makedirs(os.path.expanduser("~/.ssh"), exist_ok=True)
        key = os.path.expanduser(f"~/.ssh/id_ed25519_{rm.host.replace('.', '_')}")
        if os.path.exists(key):
            key += "_new"
        if subprocess.run(["ssh-keygen", "-t", "ed25519", "-C", email, "-f", key]).returncode != 0:
            return False
    pub = key + ".pub"
    try:
        with open(pub, encoding="utf-8") as f:
            print("\n  Add this PUBLIC key to your account:\n\n    " + f.read().strip() + "\n")
    except OSError:
        return False
    if "github" in rm.host:
        print("  GitHub: https://github.com/settings/ssh/new")
        try:
            webbrowser.open("https://github.com/settings/ssh/new")
        except Exception:
            pass
    ask("  Press Enter once you've added it... ")
    h, o = ssh_identity(rm.host, rm.user or "git", key=key, port=rm.port)
    if o != "ok":
        err("  The host still doesn't accept that key.")
        return False
    use_ssh_key(key)
    return True


def switch_account(rm: Remote, lg: Login) -> bool:
    if lg.method == "gh":
        others = [a for a in gh_accounts(rm.host) if a != lg.handle]
        if others:
            for i, a in enumerate(others, 1):
                print(f"   {i}. @{a}")
            pick = choose(f"  Switch to which account? [1-{len(others)}] (0 = sign in with a new one): ",
                          "0" + "".join(str(i) for i in range(1, min(len(others), 9) + 1)))
            if pick != "0":
                return subprocess.run(["gh", "auth", "switch", "--hostname", rm.host, "--user", others[int(pick) - 1]]).returncode == 0
        return flow_gh(rm)
    if lg.method == "ssh":
        return flow_ssh(rm, switching=True)
    return flow_token(rm, old_handle=lg.handle)


def login_menu(rm: Remote, lg: Login) -> str:
    """Returns 'retry' | 'later' | 'skip'."""
    gh_ok = "github" in rm.host
    print("\nHow do you want to sign in?")
    print("  [1] Browser login with the GitHub CLI" + ("" if gh_ok and shutil.which("gh") else
          "   (needs `gh` from cli.github.com)" if gh_ok else "   (GitHub hosts only)"))
    print("  [2] Personal access token (HTTPS)")
    print("  [3] SSH key")
    print("  [4] Later - let git ask me when I push")
    print("  [5] Skip - commit only, don't push")
    while True:
        c = choose("Choose [1-5]: ", "12345")
        if c == "1" and flow_gh(rm):
            return "retry"
        if c == "2" and flow_token(rm, old_handle=lg.handle):
            return "retry"
        if c == "3" and flow_ssh(rm):
            return "retry"
        if c == "4":
            return "later"
        if c == "5":
            return "skip"
        if c in "123":
            print("That didn't work out. Pick another option.")


def scrub_url(rm: Remote) -> Optional[Remote]:
    clean = re.sub(r"(://)[^@/]+@", r"\1", rm.url)
    if git("remote", "set-url", rm.name, clean).rc == 0:
        ok("Removed the password from the remote URL.")
        return pick_remote()
    return rm


def handle_url_secret(args, rm: Remote) -> Remote:
    warn("⚠ Your remote URL contains a password/token in plain text (it sits in .git/config).")
    if args.yes or args.dry_run:
        dim("  Run without -y to fix it.")
        return rm
    c = choose("[m] move it into the credential helper and clean the URL  [c] just clean the URL  "
               "[i] ignore: ", "mci", "m")
    if c == "m":
        store_credential(rm, rm.user or "git", rm.secret or "")
        return scrub_url(rm) or rm
    if c == "c":
        return scrub_url(rm) or rm
    return rm


def login_step(args, rm: Optional[Remote]) -> None:
    S.remote = rm
    if args.no_login_check or rm is None or rm.scheme == "local":
        return
    if rm.secret:
        rm = handle_url_secret(args, rm)
        S.remote = rm
    lg = detect_login(rm)
    asked = False
    for _ in range(4):
        show_login(lg, rm)
        if lg.status == "ok":
            if lg.handle and not (asked or args.yes or args.dry_run or args.keep_account):
                if args.switch_account:
                    c = "d"
                else:
                    c = choose(f"Keep using @{lg.handle}? [Y]es / [d]ifferent account: ", "yd", "y")
                if c == "d":
                    asked = True
                    if switch_account(rm, lg):
                        rm = pick_remote() or rm
                        S.remote = rm
                        lg = detect_login(rm)
                        continue
                    print("Switching didn't work; keeping the current account.")
            return
        if lg.status in ("offline", "unknown"):
            return
        # no_auth / no_write
        if args.yes or args.dry_run:
            S.push_blocked = True
            warn("Pushing will be skipped (run without -y to sign in).")
            return
        action = login_menu(rm, lg)
        if action == "later":
            S.login_deferred = True
            return
        if action == "skip":
            S.push_blocked = True
            return
        rm = pick_remote() or rm
        S.remote = rm
        lg = detect_login(rm)
        asked = True
    S.push_blocked = True
    warn("Still not signed in - pushing will be skipped this time.")


# ----------------------------------------------------------------------------
# Push
# ----------------------------------------------------------------------------
def run_push(cmd: List[str], label: str, rm: Remote, pull_args: List[str], args) -> bool:
    attempts, rebased = 0, False
    while True:
        print(f"Pushing to {label}...")
        r = git(*cmd)
        if r.rc == 0:
            if r.text.strip():
                dim(clip(r.text, 4))
            ok(f"Pushed to {label}")
            return True
        low = r.text.lower()
        print(clip(r.text, 6))
        if any(k in low for k in ("non-fast-forward", "fetch first", "tip of your current branch is behind", "[rejected]")) \
                and "protected branch" not in low and not rebased:
            warn("The remote has commits you don't have yet.")
            if args.yes:
                return False
            if choose("[r] pull --rebase, then push again  [s] stop: ", "rs", "r") == "s":
                return False
            rebased = True
            git_live("pull", "--rebase", "--autostash", *pull_args)
            if unmerged_entries() or in_progress():
                if conflict_session(args) != "resolved":
                    return False
            continue
        if "protected branch" in low or "gh006" in low or "gh013" in low:
            err("That branch is protected - direct pushes aren't allowed.")
            print("Run again and push to another branch (choose A), then open a pull request.")
            return False
        if "gh001" in low or "large files detected" in low or "exceeds github's file size" in low:
            err("GitHub rejected a file because it is too large.")
            print(f"Undo this run with  {tool_cmd('singulate_pro.py')} --undo , remove the big file (or use Git LFS), and try again.")
            return False
        state, _, _ = classify(r)
        if state in ("no_auth", "no_write") and not args.yes and attempts < 1:
            attempts += 1
            warn("The push was refused because of your login.")
            lg = Login(state, None, "")
            if login_menu(rm, lg) == "retry":
                continue
            return False
        if state == "offline" and attempts < 3:
            attempts += 1
            wait = 2 ** attempts
            warn(f"Network problem - retrying in {wait}s ({attempts}/3)...")
            time.sleep(wait)
            continue
        err("Push failed.")
        return False


def push_step(args, count: int, st: Optional[dict]) -> Optional[str]:
    if count <= 0:
        return None
    branch = current_branch()
    if args.dry_run:
        dim("[dry-run] would ask whether to push")
        return None
    if S.push_blocked:
        warn("Not pushing: you're not signed in (or chose to skip).")
        return None
    if args.push:
        choice = "y"
    elif args.no_push or args.yes:
        return None
    else:
        shown = branch or "detached HEAD"
        choice = choose(f"Push?  [n] no   [y] current branch ('{shown}')   [A] another branch : ", "nya", "n")
    if choice == "n":
        return None
    rm = S.remote or pick_remote()
    if not rm:
        err("No remote configured. Add one with: git remote add origin <url>")
        return None
    if choice == "y":
        if not branch:
            err("You're on a detached HEAD. Re-run and choose A to name a branch.")
            return None
        up = upstream()
        if up:
            cmd, remote_branch = ["push"], up.split("/", 1)[-1]
        else:
            cmd, remote_branch = ["push", "-u", rm.name, branch], branch
        target = branch
    else:
        name = ask("Branch name: ")
        if not name or git("check-ref-format", "--branch", name).rc != 0:
            err("That isn't a valid branch name.")
            return None
        cmd, remote_branch, target = ["push", rm.name, f"HEAD:refs/heads/{name}"], name, name
    label = f"{rm.name}/{target}"
    if run_push(cmd, label, rm, [rm.name, remote_branch], args):
        if st is not None:
            st["pushed"] = True
            save_state(st)
        return label
    return None


# ----------------------------------------------------------------------------
# Doctor
# ----------------------------------------------------------------------------
def run_doctor(args) -> int:
    bold(f"Singulate Pro {VERSION} - doctor (read-only)")
    gv = ".".join(str(x) for x in git_version())
    rows = [("git", gv), ("python", platform.python_version()), ("system", f"{platform.system()} {platform.release()}"),
            ("repository", S.root)]
    b = current_branch()
    rows.append(("branch", b or "DETACHED HEAD"))
    rows.append(("HEAD", (head_sha() or "no commits yet")[:10]))
    up = upstream()
    if up:
        a, bh = ahead_behind()
        rows.append(("upstream", f"{up}  (ahead {a}, behind {bh})"))
    else:
        rows.append(("upstream", "none"))
    rows.append(("in progress", in_progress() or "nothing"))
    rows.append(("conflicts", str(len(unmerged_entries()))))
    rows.append(("stashes", str(len([l for l in git("stash", "list").out.splitlines() if l.strip()]))))
    name, email = git("config", "user.name").out.strip(), git("config", "user.email").out.strip()
    rows.append(("commit identity", f"{name} <{email}>" if name and email else "NOT SET"))
    rows.append(("commit signing", git("config", "commit.gpgsign").out.strip() or "off"))
    rows.append(("line endings", f"autocrlf={git('config', 'core.autocrlf').out.strip() or 'unset'}"))
    hooks = os.path.join(S.git_dir, "hooks")
    active = [h for h in (os.listdir(hooks) if os.path.isdir(hooks) else []) if not h.endswith(".sample")]
    rows.append(("hooks", ", ".join(active) or "none"))
    rows.append(("submodules", str(len(_gitlinks()))))
    try:
        with open(os.path.join(S.root, ".gitattributes"), encoding="utf-8", errors="ignore") as f:
            lfs = "filter=lfs" in f.read()
    except OSError:
        lfs = False
    rows.append(("git-lfs", ("used, " + ("installed" if git("lfs", "version").rc == 0 else "NOT INSTALLED")) if lfs else "not used"))
    st = load_state()
    rows.append(("last run", f"{len(st['commits'])} commit(s), pushed={st.get('pushed')}" if st and st.get("commits") else "none"))
    rm = pick_remote()
    if rm:
        rows.append(("remote", f"{rm.name} -> {redact_url(rm.url)}"))
        if rm.secret:
            rows.append(("WARNING", "the remote URL contains a password/token"))
    else:
        rows.append(("remote", "none"))
    for k, v in rows:
        print(f"  {k:<16} {v}")
    if rm and rm.scheme != "local" and not args.no_login_check:
        print("  checking login...")
        lg = detect_login(rm)
        show_login(lg, rm)
    return 0


# ----------------------------------------------------------------------------
# Summary + main
# ----------------------------------------------------------------------------
def summary(done, failed, skipped, noop, pushed, st, dry=False) -> None:
    print()
    bold("Summary" + (" (dry run)" if dry else ""))
    counts: Dict[str, int] = {}
    for c in done:
        counts[c.kind] = counts.get(c.kind, 0) + 1
    parts = [f"{n} {k}" for k, n in counts.items()]
    print(f"  Committed:  {len(done)}" + (f"  ({', '.join(parts)})" if parts else ""))
    if skipped:
        print(f"  Skipped:    {skipped}")
    if noop:
        print(f"  No change:  {len(noop)}  (nothing effective to commit)")
    if S.conflicts_resolved:
        print(f"  Conflicts resolved: {S.conflicts_resolved}")
    if failed:
        print(paint(f"  Failed:     {len(failed)}", "31"))
        for c, why in failed:
            print(f"     - {c.shown}: {why}")
    if S.login_line:
        print(f"  Account:    {S.login_line}")
    if pushed:
        print(f"  Pushed to:  {pushed}")
    for n in S.notes:
        print(f"  Note:       {n}")
    if done and not dry and not pushed:
        dim(f"  Changed your mind? {tool_cmd('singulate_pro.py')} --undo")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Singulate Pro - one commit per file, even when git gets complicated.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--pull", action="store_true", help="pull without asking")
    g.add_argument("--no-pull", action="store_true", help="don't pull, don't ask")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--push", action="store_true", help="push the current branch without asking")
    g.add_argument("--no-push", action="store_true", help="don't push, don't ask")
    p.add_argument("-y", "--yes", action="store_true",
                   help="no questions (never resolves conflicts, never logs in, auto-skips secrets)")
    p.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    p.add_argument("--prefix", default="", help='message prefix, e.g. "feat:"')
    p.add_argument("--doctor", action="store_true", help="read-only report of repo and login state")
    p.add_argument("--undo", action="store_true", help="undo the last run (only if it wasn't pushed)")
    p.add_argument("--switch-account", action="store_true", help="go straight to choosing a different account")
    p.add_argument("--keep-account", action="store_true", help="don't ask whether to keep the current account")
    p.add_argument("--no-login-check", action="store_true", help="skip the login check")
    p.add_argument("--no-content-scan", action="store_true", help="don't scan file contents for secrets")
    p.add_argument("--include-self", action="store_true",
                   help="also commit Singulate's own script files (skipped by default)")
    p.add_argument("--group-threshold", type=int, default=GROUP_THRESHOLD,
                   help=f"offer folder grouping above this many files (default {GROUP_THRESHOLD})")
    p.add_argument("-v", "--verbose", action="store_true", help="print every git command")
    p.add_argument("--version", action="version", version=f"Singulate Pro {VERSION}")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    init_repo_context()
    S.verbose = args.verbose
    if args.doctor:
        return run_doctor(args)
    if args.undo:
        return run_undo(args)

    done: List[Change] = []
    failed: List[Tuple[Change, str]] = []
    noop: List[Change] = []
    skipped, pushed, st = 0, None, None
    try:
        if git_version() < (2, 13, 0):
            warn("Your git is old (< 2.13); some features may not work. Please update git.")
        if not startup_operation(args):
            return 1
        login_step(args, pick_remote())
        if not pull_step(args):
            return 1

        changes, nested = get_changes(args)
        for n in nested:
            warn(f"Skipping {n}")
        if not changes:
            ok("Nothing to commit - working tree clean.")
            ahead = ahead_behind()[0] if upstream() else 0
            if ahead:
                print(f"But {ahead} local commit(s) haven't been pushed yet.")
                pushed = push_step(args, ahead, load_state())
            return 0
        if not ensure_branch(args) or not ensure_identity(args):
            return 1

        selected = review(changes, args)
        skipped = len(changes) - len(selected)
        if not selected:
            print("Nothing selected. Bye.")
            return 0

        st = start_record(args)
        prefix = (args.prefix.rstrip() + " ") if args.prefix else ""
        for c in selected:
            msg = c.message(prefix)
            if args.dry_run:
                dim(f"[dry-run] git add -- {' '.join(c.add_paths[:3])}{' ...' if len(c.add_paths) > 3 else ''}")
                dim(f"[dry-run] git commit -m \"{msg}\" -- ...")
                done.append(c)
                continue
            res, detail = stage_and_commit(c, msg, args)
            if res == "ok":
                done.append(c)
                st["commits"].append({"sha": head_sha(), "path": c.shown})
                save_state(st)
                ok(f"✔ {msg}")
            elif res == "noop":
                noop.append(c)
                dim(f"- {c.shown}: {detail}")
            else:
                failed.append((c, detail))
                err(f"✘ {c.shown}: {detail}")
        if done and not args.dry_run:
            st["finished"] = True
            save_state(st)
            pushed = push_step(args, len(done), st)
    except KeyboardInterrupt:
        print("\n\nInterrupted - stopping safely. Run again to continue; --undo is available.")
        summary(done, failed, skipped, noop, pushed, st, args.dry_run)
        return 130

    summary(done, failed, skipped, noop, pushed, st, args.dry_run)
    return 1 if failed else 0


if __name__ == "__main__":
    run_as_script(main)
