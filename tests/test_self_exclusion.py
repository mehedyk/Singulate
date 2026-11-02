"""
The Singulate scripts must never commit themselves into the user's repository.
These tests run the REAL scripts as subprocesses inside throwaway repos.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, ".."))
ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
           PYTHONDONTWRITEBYTECODE="1")
SCRIPTS = ("simple_push.py", "singulate.py", "singulate_pro.py")


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, env=ENV, capture_output=True, text=True).stdout.strip()


def run_script(repo, rel_script, *args, stdin=""):
    return subprocess.run([sys.executable, rel_script, *args], cwd=repo, env=ENV, input=stdin,
                          capture_output=True, text=True, timeout=120)


def flags(script):
    if script == "singulate_pro.py":
        return ["-y", "--no-push", "--no-pull", "--no-login-check"]
    if script == "singulate.py":
        return ["-y", "--no-push", "--no-pull"]
    return []


class SelfExclusion(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, True)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "user.email", "t@t.t")
        with open(os.path.join(self.repo, "seed.txt"), "w") as f:
            f.write("seed\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")

    def install(self, script, subdir=""):
        d = os.path.join(self.repo, subdir)
        os.makedirs(d, exist_ok=True)
        shutil.copy(os.path.join(SRC, script), os.path.join(d, script))
        return os.path.join(subdir, script) if subdir else script

    def write(self, name, text="x\n"):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)

    def committed_files(self):
        return set(git(self.repo, "ls-tree", "-r", "HEAD", "--name-only").splitlines())

    def run_it(self, script, rel, *extra):
        stdin = "n\n\n" if script == "simple_push.py" else ""      # simple: pull? n, default message
        return run_script(self.repo, rel, *flags(script), *extra, stdin=stdin)

    def test_scripts_do_not_commit_themselves(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                self.setUp()
                rel = self.install(script)
                self.write("app.py")
                r = self.run_it(script, rel)
                files = self.committed_files()
                self.assertIn("app.py", files, r.stdout + r.stderr)
                self.assertNotIn(script, files, r.stdout + r.stderr)

    def test_all_sibling_versions_are_skipped(self):
        self.install("singulate.py")
        self.install("singulate_pro.py")
        self.install("simple_push.py")
        self.write("app.py")
        self.run_it("singulate.py", "singulate.py")
        files = self.committed_files()
        self.assertIn("app.py", files)
        for s in SCRIPTS:
            self.assertNotIn(s, files)

    def test_script_in_a_subfolder_is_skipped_too(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                self.setUp()
                rel = self.install(script, "tools")
                self.write("app.py")
                self.run_it(script, rel)
                files = self.committed_files()
                self.assertIn("app.py", files)
                self.assertNotIn("tools/" + script, files)

    def test_include_self_flag_commits_them(self):
        for script in ("singulate.py", "singulate_pro.py"):
            with self.subTest(script=script):
                self.setUp()
                rel = self.install(script)
                self.run_it(script, rel, "--include-self")
                self.assertIn(script, self.committed_files())

    def test_only_the_tool_changed_means_nothing_to_commit(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                self.setUp()
                rel = self.install(script)
                before = git(self.repo, "rev-parse", "HEAD")
                r = self.run_it(script, rel)
                self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before, r.stdout)

    def test_tool_already_tracked_by_the_user_is_treated_normally(self):
        # the user (e.g. in the Singulate repo itself) deliberately tracks the file: edits are committed
        for script in ("singulate.py", "singulate_pro.py"):
            with self.subTest(script=script):
                self.setUp()
                rel = self.install(script)
                git(self.repo, "add", rel)
                git(self.repo, "commit", "-qm", "track tool")
                with open(os.path.join(self.repo, rel), "a") as f:
                    f.write("\n# my edit\n")
                self.run_it(script, rel)
                self.assertIn(f"updated {script}", git(self.repo, "log", "-3", "--format=%s"))


if __name__ == "__main__":
    unittest.main()
