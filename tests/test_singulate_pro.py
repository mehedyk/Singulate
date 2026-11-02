"""
Tests for singulate_pro.py. They build real throwaway git repositories (no network),
including real merge / rebase conflicts.   Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import singulate_pro as sp  # noqa: E402

ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
           GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@t.t", GIT_COMMITTER_NAME="T",
           GIT_COMMITTER_EMAIL="t@t.t", GIT_TERMINAL_PROMPT="0")


def sh(cwd, *cmd, check=True):
    p = subprocess.run(cmd, cwd=cwd, env=ENV, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{cmd}: {p.stderr}{p.stdout}")
    return p.stdout.strip()


def write(cwd, rel, text="x\n"):
    p = os.path.join(cwd, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def read(cwd, rel):
    with open(os.path.join(cwd, rel), encoding="utf-8", newline="") as f:
        return f.read()


WIN = os.name == "nt"
unix_only = unittest.skipIf(WIN, "uses shell-script fakes / unix file modes")


class Args:
    """Stand-in for parsed command-line args."""
    def __init__(self, **kw):
        base = dict(pull=False, no_pull=False, push=False, no_push=False, yes=False, dry_run=False,
                    prefix="", doctor=False, undo=False, switch_account=False, keep_account=True,
                    no_login_check=True, no_content_scan=False, group_threshold=200, verbose=False, include_self=False)
        base.update(kw)
        self.__dict__.update(base)


class Base(unittest.TestCase):
    def setUp(self):
        for k, v in ENV.items():
            os.environ[k] = v

        def quiet_live(*args, env=None):          # keep git's own chatter out of the test output
            return subprocess.run(["git", "--literal-pathspecs", *args], cwd=sp.S.root or None, env=sp._env(env),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        patcher = mock.patch.object(sp, "git_live", quiet_live)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.remote = os.path.join(self.tmp, "remote.git")
        sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main", self.remote)
        self.a = self.clone("alice")
        write(self.a, "base.txt", "line1\nline2\nline3\n")
        write(self.a, "keep.txt", "keep\n")
        sh(self.a, "git", "add", ".")
        sh(self.a, "git", "commit", "-qm", "init")
        sh(self.a, "git", "push", "-q", "-u", "origin", "main")
        self.use(self.a)

    def clone(self, name):
        d = os.path.join(self.tmp, name)
        sh(self.tmp, "git", "clone", "-q", self.remote, d)
        sh(d, "git", "config", "user.name", name)
        sh(d, "git", "config", "user.email", f"{name}@t.t")
        sh(d, "git", "checkout", "-q", "-B", "main") if not os.path.exists(os.path.join(d, ".git", "refs", "heads", "main")) else None
        sh(d, "git", "branch", "-q", "--set-upstream-to=origin/main", "main", check=False)
        return d

    def use(self, repo):
        sp.S.__init__()          # fresh global state
        sp.S.root = repo
        sp.S.git_dir = sh(repo, "git", "rev-parse", "--absolute-git-dir")
        self.repo = repo

    def run_main(self, *argv, answers=()):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(sp, "init_repo_context", lambda: None), \
                mock.patch.object(sp, "ask", side_effect=list(answers) + [""] * 20) as _a, \
                mock.patch.object(sp, "yes_no", side_effect=lambda *a, **k: True):
            rc = sp.main(["--no-login-check", *argv])
        self.out = out.getvalue()
        return rc

    def log(self, repo=None, n=20):
        return sh(repo or self.repo, "git", "log", f"-{n}", "--format=%s").splitlines()

    def files_in(self, rev, repo=None):
        return sh(repo or self.repo, "git", "show", "--name-only", "--format=", rev).splitlines()


class TestBasics(Base):
    def test_every_kind_one_commit_each(self):
        write(self.a, "keep.txt", "changed\n")                 # updated
        write(self.a, "new file with space.txt")               # created, space in name
        write(self.a, "dir/sub/deep.py", "print(1)\n")         # created in new folder
        write(self.a, "star*.txt")                              # '*' in a file name
        os.remove(os.path.join(self.a, "base.txt"))            # deleted
        self.assertEqual(self.run_main("--no-push", "-y"), 0, self.out)
        msgs = self.log()
        for want in ("updated keep.txt", "created new file with space.txt", "created dir/sub/deep.py",
                     "created star*.txt", "deleted base.txt"):
            self.assertIn(want, msgs)
        # each commit touches exactly its own file
        for rev in sh(self.a, "git", "rev-list", "-5", "HEAD").splitlines():
            self.assertEqual(len(self.files_in(rev)), 1, rev)
        self.assertEqual(sh(self.a, "git", "status", "--porcelain"), "")

    @unix_only
    def test_rename_and_mode_change(self):
        sh(self.a, "git", "mv", "keep.txt", "kept.txt")
        os.chmod(os.path.join(self.a, "base.txt"), 0o755)
        self.run_main("--no-push", "-y")
        msgs = self.log()
        self.assertIn("renamed keep.txt -> kept.txt", msgs)
        self.assertIn("changed mode of base.txt", msgs)

    def test_prefix(self):
        write(self.a, "p.txt")
        self.run_main("--no-push", "-y", "--prefix", "feat:")
        self.assertEqual(self.log()[0], "feat: created p.txt")

    def test_unborn_repo_first_commits(self):
        d = os.path.join(self.tmp, "fresh")
        os.makedirs(d)
        sh(d, "git", "init", "-q")
        sh(d, "git", "config", "user.name", "x")
        sh(d, "git", "config", "user.email", "x@x.x")
        write(d, "a.txt")
        write(d, "b.txt")
        self.use(d)
        self.assertEqual(self.run_main("--no-push", "-y"), 0, self.out)
        self.assertEqual(sorted(self.log(d)), ["created a.txt", "created b.txt"])

    def test_nested_repo_is_skipped(self):
        d = os.path.join(self.a, "vendor", "lib")
        os.makedirs(d)
        sh(d, "git", "init", "-q")
        write(d, "x.txt")
        write(self.a, "ok.txt")
        self.run_main("--no-push", "-y")
        self.assertIn("created ok.txt", self.log())
        self.assertIn("own .git", self.out)

    def test_dry_run_changes_nothing(self):
        write(self.a, "z.txt")
        before = sh(self.a, "git", "rev-parse", "HEAD")
        self.run_main("--dry-run", "-y")
        self.assertEqual(sh(self.a, "git", "rev-parse", "HEAD"), before)

    def test_many_files_grouped_by_folder(self):
        for i in range(6):
            write(self.a, f"pkg/f{i}.txt", str(i))
        write(self.a, "solo.txt")
        self.run_main("--no-push", "-y", "--group-threshold", "3")
        msgs = self.log()
        self.assertIn("created 6 files in pkg/", msgs)
        self.assertIn("created solo.txt", msgs)


class TestSecrets(Base):
    def test_secret_name_and_content_are_excluded(self):
        write(self.a, ".env", "A=1\n")
        write(self.a, ".env.example", "A=\n")
        write(self.a, "cfg.py", 'key = "AKIAABCDEFGHIJKLMNOP"\n')
        write(self.a, "fine.txt")
        self.run_main("--no-push", "-y")
        msgs = self.log()
        self.assertIn("created fine.txt", msgs)
        self.assertIn("created .env.example", msgs)
        self.assertNotIn("created .env", msgs)
        self.assertNotIn("created cfg.py", msgs)

    def test_interactive_can_include_anyway(self):
        write(self.a, ".env", "A=1\n")
        self.run_main("--no-push", answers=["i", "y"])
        self.assertIn("created .env", self.log())


class TestConflicts(Base):
    def diverge(self, a_text, b_text, path="base.txt"):
        b = self.clone("bob")
        write(b, path, b_text)
        sh(b, "git", "commit", "-qam", "bob edit")
        sh(b, "git", "push", "-q")
        write(self.a, path, a_text)
        sh(self.a, "git", "commit", "-qam", "alice edit")
        return b

    def test_merge_conflict_keep_yours(self):
        self.diverge("line1\nALICE\nline3\n", "line1\nBOB\nline3\n")
        write(self.a, "later.txt")
        # answers: merge, resolve with [y]ours
        rc = self.run_main("--pull", "--no-push", answers=["m", "y", ""])
        self.assertEqual(rc, 0, self.out)
        self.assertIn("ALICE", read(self.a, "base.txt"))
        self.assertNotIn("<<<<", read(self.a, "base.txt"))
        self.assertIsNone(sp.in_progress())
        self.assertIn("created later.txt", self.log())
        self.assertEqual(sp.S.conflicts_resolved, 1)

    def test_merge_conflict_take_theirs(self):
        self.diverge("line1\nALICE\nline3\n", "line1\nBOB\nline3\n")
        self.run_main("--pull", "--no-push", answers=["m", "t"])
        self.assertIn("BOB", read(self.a, "base.txt"))

    def test_merge_conflict_keep_both(self):
        self.diverge("line1\nALICE\nline3\n", "line1\nBOB\nline3\n")
        self.run_main("--pull", "--no-push", answers=["m", "b"])
        t = read(self.a, "base.txt")
        self.assertIn("ALICE", t)
        self.assertIn("BOB", t)
        self.assertNotIn("<<<<", t)

    def test_rebase_flips_yours_and_theirs(self):
        # during a rebase git's "ours" is the upstream, but "yours" must still mean MY commit
        self.diverge("line1\nALICE\nline3\n", "line1\nBOB\nline3\n")
        self.run_main("--pull", "--no-push", answers=["r", "y"])
        self.assertIn("ALICE", read(self.a, "base.txt"))
        self.assertNotIn("BOB", read(self.a, "base.txt"))
        self.assertIsNone(sp.in_progress())
        self.assertIn("alice edit", self.log())

    def test_edit_then_confirm_resolves(self):
        self.diverge("line1\nALICE\nline3\n", "line1\nBOB\nline3\n")

        def fake_editor(path):
            write(self.a, path, "line1\nBOTH HAPPY\nline3\n")
            return True
        with mock.patch.object(sp, "open_editor", fake_editor):
            self.run_main("--pull", "--no-push", answers=["m", "e"])
        self.assertEqual(read(self.a, "base.txt"), "line1\nBOTH HAPPY\nline3\n")

    def test_markers_left_in_file_are_refused(self):
        self.diverge("line1\nALICE\nline3\n", "line1\nBOB\nline3\n")
        # [r] "I fixed it" while markers are still there -> refused, then take theirs
        self.run_main("--pull", "--no-push", answers=["m", "r", "t"])
        self.assertIn("still in the file", self.out)
        self.assertIn("BOB", read(self.a, "base.txt"))

    def test_modify_delete_conflict_keep_and_remove(self):
        b = self.clone("bob")
        sh(b, "git", "rm", "-q", "keep.txt")
        sh(b, "git", "commit", "-qm", "bob deletes")
        sh(b, "git", "push", "-q")
        write(self.a, "keep.txt", "alice changed it\n")
        sh(self.a, "git", "commit", "-qam", "alice modifies")
        self.run_main("--pull", "--no-push", answers=["m", "k"])
        self.assertEqual(read(self.a, "keep.txt"), "alice changed it\n")

    def test_yes_mode_aborts_instead_of_guessing(self):
        self.diverge("line1\nALICE\nline3\n", "line1\nBOB\nline3\n")
        rc = self.run_main("--pull", "-y")           # -y refuses to pick merge/rebase
        self.assertEqual(rc, 1)
        self.assertIsNone(sp.in_progress())

    def test_abort_everything_restores_state(self):
        self.diverge("line1\nALICE\nline3\n", "line1\nBOB\nline3\n")
        before = sh(self.a, "git", "rev-parse", "HEAD")
        self.run_main("--pull", "--no-push", answers=["m", "a"])
        self.assertIsNone(sp.in_progress())
        self.assertEqual(sh(self.a, "git", "rev-parse", "HEAD"), before)
        self.assertIn("ALICE", read(self.a, "base.txt"))

    def test_autostash_conflict_is_resolved_and_changes_kept(self):
        b = self.clone("bob")
        write(b, "base.txt", "line1\nBOB\nline3\n")
        sh(b, "git", "commit", "-qam", "bob edit")
        sh(b, "git", "push", "-q")
        write(self.a, "base.txt", "line1\nMY LOCAL EDIT\nline3\n")      # uncommitted, same line
        self.run_main("--pull", "--no-push", answers=["y"])
        self.assertIn("MY LOCAL EDIT", read(self.a, "base.txt"))
        self.assertEqual(sh(self.a, "git", "stash", "list"), "")
        self.assertIn("updated base.txt", self.log())

    def test_operation_already_in_progress_is_resumed(self):
        self.diverge("line1\nALICE\nline3\n", "line1\nBOB\nline3\n")
        sh(self.a, "git", "pull", "--no-rebase", "--no-edit", check=False)   # leaves a conflicted merge
        self.assertEqual(sp.in_progress(), "merge")
        self.run_main("--no-push", answers=["r", "y"])
        self.assertIsNone(sp.in_progress())
        self.assertIn("ALICE", read(self.a, "base.txt"))


class TestPushAndBranches(Base):
    def test_push_current_branch(self):
        write(self.a, "n.txt")
        self.run_main(answers=["y", "y"])        # 'y' = proceed, 'y' = push current
        self.assertEqual(sh(self.a, "git", "rev-parse", "HEAD"),
                         sh(self.a, "git", "rev-parse", "origin/main"))

    def test_push_to_another_branch(self):
        write(self.a, "n.txt")
        self.run_main(answers=["y", "a", "feature-x"])
        remote_heads = sh(self.a, "git", "ls-remote", "--heads", "origin")
        self.assertIn("refs/heads/feature-x", remote_heads)
        self.assertEqual(sh(self.a, "git", "rev-parse", "origin/main"), sh(self.a, "git", "rev-parse", "HEAD~1"))

    def test_rejected_push_offers_rebase_and_retries(self):
        b = self.clone("bob")
        write(b, "bobfile.txt")
        sh(b, "git", "add", "."); sh(b, "git", "commit", "-qm", "bob adds file"); sh(b, "git", "push", "-q")
        write(self.a, "mine.txt")
        self.run_main(answers=["y", "y", "r"])
        self.assertEqual(sh(self.a, "git", "rev-parse", "HEAD"), sh(self.a, "git", "rev-parse", "origin/main"))
        self.assertIn("bob adds file", self.log())
        self.assertIn("created mine.txt", self.log())

    def test_detached_head_creates_branch(self):
        sh(self.a, "git", "checkout", "-q", "--detach")
        write(self.a, "d.txt")
        self.run_main("--no-push", answers=["b", "rescue", "y"])
        self.assertEqual(sh(self.a, "git", "branch", "--show-current"), "rescue")
        self.assertIn("created d.txt", self.log())

    def test_unpushed_commits_are_offered_when_tree_is_clean(self):
        write(self.a, "q.txt")
        sh(self.a, "git", "add", "."); sh(self.a, "git", "commit", "-qm", "local only")
        self.run_main(answers=["y"])
        self.assertEqual(sh(self.a, "git", "rev-parse", "HEAD"), sh(self.a, "git", "rev-parse", "origin/main"))


class TestRecovery(Base):
    def test_missing_identity_is_asked_for_and_saved_locally(self):
        sh(self.a, "git", "config", "--unset", "user.name")
        sh(self.a, "git", "config", "--unset", "user.email")
        for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            os.environ.pop(k, None)
        write(self.a, "i.txt")
        self.run_main("--no-push", answers=["Ann", "ann@x.org", "y"])
        self.assertEqual(sh(self.a, "git", "config", "--local", "user.name"), "Ann")
        self.assertIn("created i.txt", self.log())

    @unix_only
    def test_failing_hook_is_reported_not_crashing(self):
        hook = os.path.join(self.a, ".git", "hooks", "pre-commit")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho lint failed >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        write(self.a, "h.txt")
        rc = self.run_main("--no-push", "-y")
        self.assertEqual(rc, 1)
        self.assertIn("hook", self.out)
        self.assertNotIn("created h.txt", self.log())

    def test_stale_lock_file_is_removed_after_consent(self):
        lock = os.path.join(sp.S.git_dir, "index.lock")
        open(lock, "w").close()
        os.utime(lock, (0, 0))               # make it look old
        write(self.a, "l.txt")
        self.run_main("--no-push", answers=["y"])
        self.assertFalse(os.path.exists(lock))
        self.assertIn("created l.txt", self.log())


class TestUndo(Base):
    def test_undo_restores_uncommitted_state(self):
        write(self.a, "u1.txt")
        write(self.a, "u2.txt")
        before = sh(self.a, "git", "rev-parse", "HEAD")
        self.run_main("--no-push", "-y")
        self.assertNotEqual(sh(self.a, "git", "rev-parse", "HEAD"), before)
        self.run_main("--undo", "-y")
        self.assertEqual(sh(self.a, "git", "rev-parse", "HEAD"), before)
        self.assertTrue(os.path.exists(os.path.join(self.a, "u1.txt")))
        self.assertIn("?? u1.txt", sh(self.a, "git", "status", "--porcelain"))

    def test_undo_refused_after_push(self):
        write(self.a, "u3.txt")
        self.run_main("-y", "--push")
        head = sh(self.a, "git", "rev-parse", "HEAD")
        rc = self.run_main("--undo", "-y")
        self.assertEqual(rc, 1)
        self.assertEqual(sh(self.a, "git", "rev-parse", "HEAD"), head)

    def test_undo_refused_if_repo_moved_on(self):
        write(self.a, "u4.txt")
        self.run_main("--no-push", "-y")
        write(self.a, "later.txt")
        sh(self.a, "git", "add", "."); sh(self.a, "git", "commit", "-qm", "manual")
        self.assertEqual(self.run_main("--undo", "-y"), 1)

    def test_undo_first_commits_of_new_repo(self):
        d = os.path.join(self.tmp, "fresh2")
        os.makedirs(d)
        sh(d, "git", "init", "-q")
        sh(d, "git", "config", "user.name", "x"); sh(d, "git", "config", "user.email", "x@x.x")
        write(d, "a.txt")
        self.use(d)
        self.run_main("--no-push", "-y")
        self.assertEqual(self.run_main("--undo", "-y"), 0, self.out)
        self.assertEqual(sh(d, "git", "rev-parse", "-q", "--verify", "HEAD", check=False), "")
        self.assertIn("?? a.txt", sh(d, "git", "status", "--porcelain"))

    def test_backup_ref_created(self):
        write(self.a, "b1.txt")
        self.run_main("--no-push", "-y")
        self.assertIn("refs/singulate/backup-", sh(self.a, "git", "for-each-ref", "refs/singulate/"))


class TestLoginLogic(Base):
    def test_parse_remote_variants(self):
        r = sp.parse_remote("o", "https://github.com/alice/repo.git")
        self.assertEqual((r.scheme, r.host, r.owner, r.repo, r.secret), ("https", "github.com", "alice", "repo", None))
        r = sp.parse_remote("o", "git@github.com:alice/repo.git")
        self.assertEqual((r.scheme, r.host, r.owner, r.repo, r.user), ("ssh", "github.com", "alice", "repo", "git"))
        r = sp.parse_remote("o", "ssh://git@gitlab.com:2222/grp/sub/repo.git")
        self.assertEqual((r.scheme, r.port, r.owner, r.repo), ("ssh", "2222", "sub", "repo"))
        r = sp.parse_remote("o", "https://bob:ghp_SECRET@github.com/bob/x.git")
        self.assertEqual((r.user, r.secret), ("bob", "ghp_SECRET"))
        self.assertNotIn("ghp_SECRET", sp.redact_url(r.url))
        self.assertEqual(sp.parse_remote("o", "/srv/git/x.git").scheme, "local")
        self.assertEqual(sp.parse_remote("o", "C:\\repos\\x.git").scheme, "local")

    def test_ssh_greeting_parsing(self):
        self.assertEqual(sp.parse_ssh_greeting("Hi mehedyk! You've successfully authenticated, but GitHub..."), "mehedyk")
        self.assertEqual(sp.parse_ssh_greeting("Welcome to GitLab, @dev-1!"), "dev-1")
        self.assertEqual(sp.parse_ssh_greeting("authenticated via ssh key.\nYou can use git... logged in as bob."), "bob")
        self.assertIsNone(sp.parse_ssh_greeting("Permission denied (publickey)."))

    def test_classify_errors(self):
        R = sp.R
        self.assertEqual(sp.classify(R(0, "", ""))[0], "ok")
        self.assertEqual(sp.classify(R(128, "", "remote: Permission to a/b.git denied to eve.\nfatal: ... 403")),
                         ("no_write", "eve", "eve has no write access to this repository"))
        self.assertEqual(sp.classify(R(128, "", "fatal: could not read Username for 'https://github.com'"))[0], "no_auth")
        self.assertEqual(sp.classify(R(128, "", "fatal: Could not resolve host: github.com"))[0], "offline")
        self.assertEqual(sp.classify(R(124, "", "timeout"))[0], "unknown")

    def _login(self, status, handle="alice", method="gh"):
        rm = sp.parse_remote("origin", "https://github.com/alice/repo.git")
        return rm, sp.Login(status, handle, method)

    def test_logged_in_keep_account_is_settled(self):
        rm, lg = self._login("ok")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sp, "pick_remote", return_value=rm), \
                mock.patch.object(sp, "detect_login", return_value=lg), \
                mock.patch.object(sp, "ask", side_effect=["y"]):
            sp.login_step(Args(no_login_check=False, keep_account=False), rm)
        self.assertIn("Logged in to github.com as @alice", out.getvalue())
        self.assertFalse(sp.S.push_blocked)

    def test_logged_in_choose_different_account(self):
        rm, lg = self._login("ok")
        lg2 = sp.Login("ok", "bob", "gh")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sp, "pick_remote", return_value=rm), \
                mock.patch.object(sp, "detect_login", side_effect=[lg, lg2]), \
                mock.patch.object(sp, "switch_account", return_value=True) as sw, \
                mock.patch.object(sp, "ask", side_effect=["d"]):
            sp.login_step(Args(no_login_check=False, keep_account=False), rm)
        sw.assert_called_once()
        self.assertIn("@bob", out.getvalue())

    def test_not_logged_in_yes_mode_blocks_push_only(self):
        rm, lg = self._login("no_auth", None, "")
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(sp, "detect_login", return_value=lg):
            sp.login_step(Args(no_login_check=False, yes=True), rm)
        self.assertTrue(sp.S.push_blocked)

    def test_not_logged_in_menu_skip_and_later(self):
        rm, lg = self._login("no_auth", None, "")
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(sp, "detect_login", return_value=lg), \
                mock.patch.object(sp, "ask", side_effect=["5"]):
            sp.login_step(Args(no_login_check=False), rm)
        self.assertTrue(sp.S.push_blocked)
        sp.S.__init__()
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(sp, "detect_login", return_value=lg), \
                mock.patch.object(sp, "ask", side_effect=["4"]):
            sp.login_step(Args(no_login_check=False), rm)
        self.assertTrue(sp.S.login_deferred)
        self.assertFalse(sp.S.push_blocked)

    def test_not_logged_in_then_logs_in_via_flow(self):
        rm, bad = self._login("no_auth", None, "")
        good = sp.Login("ok", "alice", "credential")
        with contextlib.redirect_stdout(io.StringIO()) as out, mock.patch.object(sp, "pick_remote", return_value=rm), \
                mock.patch.object(sp, "detect_login", side_effect=[bad, good]), \
                mock.patch.object(sp, "flow_token", return_value=True), \
                mock.patch.object(sp, "ask", side_effect=["2"]):
            sp.login_step(Args(no_login_check=False, keep_account=True), rm)
        self.assertFalse(sp.S.push_blocked)

    def test_blocked_push_is_skipped_but_commits_happen(self):
        write(self.a, "w.txt")
        sp_state = sp.S
        sp_state.push_blocked = True
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sp, "init_repo_context", lambda: None), \
                mock.patch.object(sp, "login_step", lambda a, r: setattr(sp.S, "push_blocked", True)), \
                mock.patch.object(sp, "ask", side_effect=["y", "y"]):
            sp.main(["--push"])
        self.assertIn("created w.txt", self.log())
        self.assertNotEqual(sh(self.a, "git", "rev-parse", "HEAD"), sh(self.a, "git", "rev-parse", "origin/main"))

    def test_store_credential_roundtrip_with_real_helper(self):
        store = os.path.join(self.tmp, "creds")
        sh(self.a, "git", "config", "--local", "credential.helper", f"store --file={store}")
        rm = sp.parse_remote("origin", "https://example.org/o/r.git")
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(sp, "yes_no", return_value=True):
            self.assertTrue(sp.store_credential(rm, "carol", "tok123", old_handle="dave"))
        who, method = sp.https_identity(rm)
        self.assertEqual((who, method), ("carol", "credential"))
        self.assertNotIn("tok123", sp.S.login_line)

    def test_credentials_in_url_are_scrubbed(self):
        sh(self.a, "git", "remote", "set-url", "origin", "https://bob:ghp_SECRET@example.org/bob/x.git")
        rm = sp.pick_remote()
        self.assertEqual(rm.secret, "ghp_SECRET")
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(sp, "ask", side_effect=["c"]):
            rm2 = sp.handle_url_secret(Args(), rm)
        self.assertIsNone(rm2.secret)
        self.assertNotIn("ghp_SECRET", sh(self.a, "git", "remote", "get-url", "origin"))

    def test_local_remote_needs_no_login(self):
        rm = sp.pick_remote()
        self.assertEqual(rm.scheme, "local")
        lg = sp.detect_login(rm)
        self.assertEqual(lg.status, "ok")

    def test_probe_push_access_dry_run_on_local_remote_changes_nothing(self):
        rm = sp.parse_remote("origin", "https://example.org/o/r.git")
        before = sh(self.remote, "git", "for-each-ref")
        sp.probe_push_access(sp.Remote("origin", self.remote, "local"))
        self.assertEqual(sh(self.remote, "git", "for-each-ref"), before)

    def _fake_bin(self, name, body):
        d = os.path.join(self.tmp, "bin")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        env = mock.patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)

    @unix_only
    def test_ssh_identity_with_fake_ssh(self):
        self._fake_bin("ssh", 'echo "Hi mehedyk! You\'ve successfully authenticated, but GitHub does not provide shell access." >&2\nexit 1\n')
        self.assertEqual(sp.ssh_identity("github.com"), ("mehedyk", "ok"))
        self._fake_bin("ssh", 'echo "git@github.com: Permission denied (publickey)." >&2\nexit 255\n')
        self.assertEqual(sp.ssh_identity("github.com"), (None, "denied"))
        self._fake_bin("ssh", 'echo "ssh: Could not resolve hostname github.com" >&2\nexit 255\n')
        self.assertEqual(sp.ssh_identity("github.com"), (None, "offline"))

    @unix_only
    def test_gh_login_and_accounts_with_fake_gh(self):
        self._fake_bin("gh", 'case "$1" in\n api) echo octocat;;\n auth) echo "github.com"; '
                       'echo "  Logged in to github.com account octocat (keyring)"; '
                       'echo "  Logged in to github.com account hubot (keyring)";;\nesac\n')
        self.assertEqual(sp.gh_login("github.com"), "octocat")
        self.assertEqual(sp.gh_accounts("github.com"), ["octocat", "hubot"])

    @unix_only
    def test_flow_ssh_picks_the_key_that_authenticates(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".ssh"))
        for k in ("id_a", "id_b"):
            for ext in ("", ".pub"):
                write(os.path.join(home, ".ssh"), k + ext, "k")
        self._fake_bin("ssh", 'case "$*" in\n *id_b*) echo "Hi bob! You\'ve successfully authenticated" >&2; exit 1;;\n'
                       '*) echo "Permission denied (publickey)." >&2; exit 255;;\nesac\n')
        rm = sp.parse_remote("origin", "git@github.com:alice/repo.git")
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}), \
                contextlib.redirect_stdout(io.StringIO()), mock.patch.object(sp, "yes_no", return_value=True):
            self.assertTrue(sp.flow_ssh(rm))
        self.assertIn("id_b", sp.S.env["GIT_SSH_COMMAND"])
        self.assertIn("id_b", sh(self.a, "git", "config", "--local", "core.sshCommand"))

    def test_doctor_runs(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sp.run_doctor(Args(no_login_check=True))
        self.assertEqual(rc, 0)
        self.assertIn("branch", out.getvalue())
        self.assertIn("commit identity", out.getvalue())


if __name__ == "__main__":
    unittest.main()
