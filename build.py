#!/usr/bin/env python3
# Singulate - one file, one commit.
# Copyright (C) 2026 mehedyk (https://mehedy.netlify.app) - AGPL-3.0-or-later, see LICENSE.
"""
Build stand-alone executables (no Python needed to run them).

    python build.py                 # builds for the system you are on (Windows -> .exe)
    python build.py --suffix windows

Output goes to ./dist :  singulate-<os>, singulate-pro-<os>, simple-push-<os>   (+ .exe on Windows)
An executable can only be built on the operating system it is meant for,
which is why the GitHub Actions workflow builds on Windows, macOS and Linux runners.
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys

TARGETS = [("singulate.py", "singulate"), ("singulate_pro.py", "singulate-pro"), ("simple_push.py", "simple-push")]
HERE = os.path.dirname(os.path.abspath(__file__))


def default_suffix() -> str:
    return {"Windows": "windows", "Darwin": "macos"}.get(platform.system(), "linux")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Singulate executables with PyInstaller.")
    ap.add_argument("--suffix", default=default_suffix(), help="label added to the file names (default: your OS)")
    ap.add_argument("--no-install", action="store_true", help="don't pip-install PyInstaller")
    args = ap.parse_args()

    if not args.no_install:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "pyinstaller"])
    dist, work = os.path.join(HERE, "dist"), os.path.join(HERE, ".pyi-build")
    os.makedirs(dist, exist_ok=True)
    for src, name in TARGETS:
        out = f"{name}-{args.suffix}"
        print(f"==> building {out}")
        subprocess.check_call([
            sys.executable, "-m", "PyInstaller", "--onefile", "--console", "--clean", "--noconfirm",
            "--name", out, "--distpath", dist, "--workpath", work, "--specpath", work,
            os.path.join(HERE, src)])
    shutil.rmtree(work, ignore_errors=True)
    print("\nDone. Files in ./dist:")
    for f in sorted(os.listdir(dist)):
        print(f"  {f}  ({os.path.getsize(os.path.join(dist, f)) / 1_048_576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
