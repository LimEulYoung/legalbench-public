"""평가 자료의 경로·입출력, 과제 ID, 명령행 인자와 종료 코드의 공통 처리."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "data" / "records"
RUBRICS = ROOT / "data" / "rubrics"
RESULTS = ROOT / "results"
JUDGING_PROTOCOL = "item-rubric-v2"
TASK_JUDGING_PROTOCOL = "task-rubric-v2"

SUBJECTS = {"civil": "민사법", "criminal": "형사법", "public": "공법"}


def parse_task(task: str) -> tuple[int, str]:
    m = re.fullmatch(r"r(\d+)-(civil|criminal|public)", task)
    if not m:
        raise SystemExit(f"과제 id 형식 오류: {task} (예: r14-criminal)")
    return int(m.group(1)), m.group(2)


def exam_month(task: str) -> str:
    """시험 시행 연월 — 변호사시험은 2012년 1월 제1회부터 매년 1월 시행."""
    rnd, _ = parse_task(task)
    return f"{2011 + rnd}년 1월"


def subject(task: str) -> str:
    return SUBJECTS[parse_task(task)[1]]


def load_record(task: str, *, numbered: bool = False, root: Path | None = None) -> str:
    """기록 전문. 채점용 numbered=True는 원본 파일의 줄 번호를 붙인다."""
    parse_task(task)
    folder = root / "data/records" if root else RECORDS
    text = (folder / f"{task}-record.md").read_text(encoding="utf-8")
    if numbered:
        # 주석 내용만 지우고 줄 수를 보존한다. [L번호]는 원본 Markdown의 줄 번호와 같다.
        text = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group().count("\n"), text, flags=re.S)
        return "\n".join(f"[L{n}] {line}" for n, line in enumerate(text.splitlines(), 1)) + "\n"
    return re.sub(r"<!--.*?-->\n?", "", text, flags=re.S).strip() + "\n"


def rubric_file(task: str, root: Path | None = None) -> Path:
    parse_task(task)
    return (root / "data/rubrics" if root else RUBRICS) / f"{task}-record.yaml"


def load_rubric(task: str, root: Path | None = None) -> dict:
    return parse_rubric(rubric_file(task, root).read_text(encoding="utf-8"), task)


def parse_rubric(text: str, task: str) -> dict:
    """파일 이름의 과제 ID를 실행 중에 보완한다. YAML 원본은 수정하지 않는다."""
    parse_task(task)
    rb = yaml.safe_load(text)
    if not isinstance(rb, dict) or not isinstance(rb.get("meta"), dict):
        raise ValueError("루브릭은 meta를 포함한 YAML 객체여야 함")
    if rb["meta"].get("id", task) != task:
        raise ValueError(f"루브릭 과제 ID 불일치: {task}")
    rb["meta"]["id"] = task
    return rb


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def answer_file(results: Path, task: str, model: str) -> Path:
    return results / "answers" / task / f"{slug(model)}.json"


def judgment_file(results: Path, task: str, model: str, judge: str) -> Path:
    return results / "judgments" / slug(judge) / task / f"{slug(model)}.json"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)


def reject_constant(value):
    raise ValueError(f"JSON에 유한하지 않은 수가 있음: {value}")


def answer_text(path: Path) -> str:
    """생성 답안 JSON 또는 참고 답안 Markdown. 주석 제거는 전처리이며 채점이 아니다."""
    if path.suffix == ".json":
        data = read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get("content"), str):
            raise ValueError(f"문자열 content가 없는 답안: {path}")
        if data.get("error") or data.get("finish_reason", "stop") != "stop":
            raise ValueError(f"생성이 완료되지 않은 답안: {path}")
        return data["content"]
    return re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S).strip()


def write_json(path: Path, obj: dict) -> None:
    """임시 파일 작성을 마친 뒤 원본을 교체하여 불완전한 JSON 저장을 방지한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
        tmp = Path(handle.name)
        try:
            json.dump(obj, handle, ensure_ascii=False, indent=2, allow_nan=False)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def positive_int(value):
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("1 이상의 정수가 필요함")
    return n


def nonnegative_float(value):
    n = float(value)
    if not math.isfinite(n) or n < 0:
        raise argparse.ArgumentTypeError("0 이상의 유한한 수가 필요함")
    return n


def add_data_argument(parser):
    parser.add_argument("--data-root", type=Path,
                        help="data/records·data/rubrics와 참고 파일을 포함한 자료 루트 (기본: 저장소)")


def unique_names(values, label):
    if len(values) != len({slug(v) for v in values}) or any(not slug(v) or slug(v) in {".", ".."} for v in values):
        raise ValueError(f"{label}: 중복되거나 같은 파일 이름으로 변환되는 이름이 있음")


def add_cost(total, cost):
    return None if total is None or cost is None else total + cost


def cli(main):
    """명령행 종료 코드: 0 완료, 1 실행 실패, 2 인자 오류, 130 사용자 중단."""
    try:
        raise SystemExit(main() or 0)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"[{Path(sys.argv[0]).stem}] {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("실행 중단. 저장된 결과와 실행 기록을 확인하세요.", file=sys.stderr)
        raise SystemExit(130) from None


def est_tokens(text: str) -> int:
    """대략적 토큰 수 — 한글 약 1.2자, 그 밖의 글자(태그·숫자) 약 4자당 1토큰으로 어림."""
    hangul = sum(1 for ch in text if "가" <= ch <= "힣")
    return int(hangul / 1.2 + (len(text) - hangul) / 4)
