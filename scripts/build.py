"""Lay out every Lambda's files under build/, ready for Terraform to zip.

    python scripts/build.py

System functions get their package plus shared/. The agent also gets its
dependencies, downloaded as Linux wheels so the build works from Windows or
macOS. boto3 is not bundled: the Lambda runtime provides it.
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, BUILD = ROOT / "src", ROOT / "build"
SYSTEM = ["agent", "control", "executor", "approval"]
DEMO = ["api", "worker", "traffic"]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "requirements.txt")


def agent_deps():
    req = SRC / "agent" / "requirements.txt"
    digest = hashlib.sha256(req.read_bytes()).hexdigest()[:12]
    cache = BUILD / ".cache" / f"agent-deps-{digest}"
    if not cache.exists():
        print("downloading agent dependencies (Linux x86_64, Python 3.12)")
        tmp = cache.with_suffix(".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "-q", "-r", str(req), "--target", str(tmp),
            "--platform", "manylinux2014_x86_64", "--implementation", "cp", "--python-version", "3.12",
            "--only-binary=:all:", "--upgrade",
        ])
        for junk in list(tmp.rglob("__pycache__")):
            shutil.rmtree(junk, ignore_errors=True)
        tmp.rename(cache)
    return cache


def main():
    for name in SYSTEM:
        out = BUILD / name
        shutil.rmtree(out, ignore_errors=True)
        if name == "agent":
            shutil.copytree(agent_deps(), out)
        shutil.copytree(SRC / name, out / name, ignore=IGNORE, dirs_exist_ok=True)
        shutil.copytree(SRC / "shared", out / "shared", ignore=IGNORE, dirs_exist_ok=True)
    for name in DEMO:
        out = BUILD / f"demo-{name}"
        shutil.rmtree(out, ignore_errors=True)
        shutil.copytree(SRC / "demo" / name, out, ignore=IGNORE)
    size = sum(f.stat().st_size for f in (BUILD / "agent").rglob("*") if f.is_file())
    print(f"built {len(SYSTEM) + len(DEMO)} functions into {BUILD} (agent: {size / 1e6:.1f} MB unzipped)")


if __name__ == "__main__":
    main()
