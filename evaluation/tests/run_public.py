#!/usr/bin/env python3
"""공개 평가 코드와 가상 예제만 임시 폴더에 복사하여 통합 검사를 실행한다."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="legalbench-public-") as folder:
        target = Path(folder) / "evaluation"
        (target / "tests").mkdir(parents=True)
        for path in source.glob("*.py"):
            shutil.copy2(path, target / path.name)
        shutil.copytree(source / "examples", target / "examples",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("test_runtime.py", "test_generation.py", "test_guidance.py"):
            shutil.copy2(source / "tests" / name, target / "tests" / name)
        return subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover",
             "-s", "evaluation/tests", "-p", "test_*.py", "-v"],
            cwd=folder,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
