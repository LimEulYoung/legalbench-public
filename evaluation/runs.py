"""실행별 설정·코드·환경과 예정 작업의 완료 상태를 저장한다."""
from __future__ import annotations

import fcntl
import hashlib
import importlib.metadata
import platform
import subprocess
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import common


@contextmanager
def generation_lock(results, *, enabled=True):
    """같은 실험 폴더의 생성 이력에 두 프로세스가 동시에 쓰지 않게 한다."""
    if not enabled:
        yield
        return
    folder = results / "runs"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".generate.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("같은 --results 폴더에서 답안 생성이 이미 실행 중임") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def environment():
    code = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(__file__).parent.glob("*.py"))}
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent,
                                capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        commit = None
    packages = {}
    for name in ("PyYAML", "openpyxl"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {"python": platform.python_version(), "platform": platform.platform(),
            "packages": packages, "git_commit": commit, "code_sha256": code}


class Run:
    """한 번의 CLI 실행에 대응하는 기록. API 키 값은 저장하지 않는다."""

    def __init__(self, args, stage, jobs):
        self.enabled = not args.dry_run
        self.root = args.results
        self.id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self.path = self.root / "runs" / f"{self.id}-{stage}.json"
        self.data = {"schema": "experiment-run-v1", "id": self.id, "stage": stage,
                     "started": now(), "repeat": args.repeat,
                     "settings": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                     "environment": environment() if self.enabled else {},
                     "jobs": [{**job, "status": "pending"} for job in jobs]}

    def __enter__(self):
        self.save()
        return self

    def __exit__(self, exc_type, exc, traceback):
        for job in self.data["jobs"]:
            if job["status"] == "pending":
                job.update(status="failed", error="실행 중단" if exc else "완료 결과 없음")
        self.data["finished"] = now()
        self.save()

    def save(self):
        self.data["counts"] = dict(Counter(job["status"] for job in self.data["jobs"]))
        self.data["complete"] = all(job["status"] in {"completed", "skipped"} for job in self.data["jobs"])
        if self.enabled:
            common.write_json(self.path, self.data)

    def record(self, task, model, status, **details):
        job = next(j for j in self.data["jobs"] if j["task"] == task and j["model"] == model)
        job.update(status=status, **details)
        self.save()

    @property
    def failed(self):
        return any(j["status"] not in {"completed", "skipped", "preview"} for j in self.data["jobs"])


def expected_jobs(results, judge):
    """실행 이력에서 요청된 채점 대상의 합집합. 누락된 결과도 집계 대상에 남긴다."""
    jobs = set()
    for path in (results / "runs").glob("*-judge.json"):
        run = common.read_json(path)
        if run.get("settings", {}).get("judge") == judge:
            jobs.update((j["task"], j["model"]) for j in run["jobs"])
    return jobs
