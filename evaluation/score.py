#!/usr/bin/env python3
"""채점 결과 검증·재집계. 문서별 내용 상한과 재량을 적용하여 Markdown·CSV 점수표를 만든다."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import grading  # noqa: E402
import rubric  # noqa: E402
import prompts  # noqa: E402
import runs


def source_errors(j: dict, rb: dict, root=None, results=None) -> list[str]:
    """다른 기준·기록·답안으로 만든 결과가 현재 점수표에 섞이지 않게 확인한다."""
    errors = []
    task_mode = j.get("protocol") == common.TASK_JUDGING_PROTOCOL
    if (j.get("protocol") not in {common.JUDGING_PROTOCOL, common.TASK_JUDGING_PROTOCOL}
            or j.get("task") != rb["meta"]["id"]
            or j.get("grading_unit") != ("task" if task_mode else "item")
            or (rb["meta"].get("reference_guide") and not task_mode)):
        errors.append("이전 채점 방식 또는 다른 과제의 결과")
    expected = {
        "rubric_sha": common.sha(common.rubric_file(rb["meta"]["id"], root).read_text(encoding="utf-8")),
        "record_sha": common.sha(common.load_record(rb["meta"]["id"], numbered=True, root=root)),
        "prompt_template_sha": grading.template_sha() if task_mode else common.sha(prompts.JUDGE_SYSTEM + prompts.JUDGE_USER),
    }
    if task_mode:
        try:
            current = grading.load_context(rb, root)["sources"]
            if grading.source_hashes(j.get("input_sources", {})) != grading.source_hashes(current):
                errors.append("input_sources: 현재 파일과 불일치")
        except (OSError, ValueError, KeyError) as error:
            errors.append(f"기준답안·채점 규칙을 확인할 수 없음: {error}")
    for field, value in expected.items():
        if j.get(field) != value:
            errors.append(f"{field}: 현재 파일과 불일치")
    try:
        answer = common.answer_file(results, j["task"], j["model"]) if results else Path(j["answer_source"])
        if not answer.exists():
            answer = Path(j["answer_source"])
        if j.get("answer_sha") != common.sha(common.answer_text(answer)):
            errors.append("답안이 채점 후 변경됨")
    except (OSError, KeyError, ValueError, TypeError):
        errors.append("채점에 사용한 답안 원문을 확인할 수 없음")
    return errors


def saved_rubric(j, task):
    """저장된 입력과 그 해시만으로 채점 당시 루브릭을 복원한다."""
    snapshot = j.get("input_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("저장된 입력이 없는 결과. 원자료 검증에는 --source current 사용")
    if j.get("task") != task or j.get("protocol") not in {common.TASK_JUDGING_PROTOCOL, common.JUDGING_PROTOCOL}:
        raise ValueError("과제 또는 채점 프로토콜 불일치")
    unit = "task" if j["protocol"] == common.TASK_JUDGING_PROTOCOL else "item"
    if j.get("grading_unit") != unit:
        raise ValueError("채점 단위와 프로토콜 불일치")
    for field in ("rubric", "record", "answer"):
        if not isinstance(snapshot.get(field), str) or common.sha(snapshot[field]) != j.get(field + "_sha"):
            raise ValueError(f"저장된 {field}의 해시 불일치")
    if common.sha(json.dumps(j.get("messages"), ensure_ascii=False)) != j.get("prompt_sha"):
        raise ValueError("저장된 프롬프트의 해시 불일치")
    texts = snapshot.get("sources", {})
    if {k: common.sha(v) for k, v in texts.items()} != grading.source_hashes(j.get("input_sources", {})):
        raise ValueError("저장된 채점 규칙·기준답안 해시 불일치")
    rb = common.parse_rubric(snapshot["rubric"], task)
    errors = rubric.validate(rb)
    if errors:
        raise ValueError("루브릭 오류: " + "; ".join(errors))
    return rb


def judgment_errors(j: dict, rb: dict) -> list[str]:
    """누락·중복·알 수 없는 항목과 허용되지 않은 점수를 찾는다. 점수를 수정하지 않는다."""
    errors = list(j.get("validation_errors", []))
    if j.get("grading_unit") == "task":
        call = j.get("call", {})
        if call.get("error") or call.get("finish_reason") != "stop":
            errors.append("일괄 채점 호출이 정상 완료되지 않음")
    expected_issues = {issue["id"] for _, issue in rubric.issues(rb)}
    if set(j.get("issues", {})) - expected_issues:
        errors.append("알 수 없는 쟁점이 있음")
    expected_docs = {doc["id"] for doc in rb["documents"] if doc.get("discretion")}
    if set(j.get("discretion", {})) - expected_docs:
        errors.append("알 수 없는 재량 항목이 있음")
    for doc, issue in rubric.issues(rb):
        rows = j.get("issues", {}).get(issue["id"], {}).get("items", [])
        ids = [row.get("id") for row in rows]
        if len(ids) != len(set(ids)):
            errors.append(f"{issue['id']}: 항목 중복")
        expected = {item["id"] for item in rubric.items(issue)}
        if set(ids) - expected:
            errors.append(f"{issue['id']}: 알 수 없는 항목")
        by_id = {row.get("id"): row for row in rows}
        for item in rubric.items(issue):
            row = by_id.get(item["id"], {})
            if row.get("review_required"):
                errors.append(f"{item['id']}: 검토 보류 — {row.get('review_reason', '')}")
            elif row.get("id") != item["id"] or row.get("error") or not rubric.valid_score(row.get("awarded"), item):
                errors.append(f"{item['id']}: 미완료 또는 허용되지 않은 점수")
    for doc in rb["documents"]:
        if doc.get("discretion"):
            item = doc["discretion"]
            row = j.get("discretion", {}).get(doc["id"], {})
            if row.get("review_required"):
                errors.append(f"{item['id']}: 검토 보류 — {row.get('review_reason', '')}")
            elif row.get("id") != item["id"] or row.get("error") or not rubric.valid_score(row.get("awarded"), item):
                errors.append(f"{item['id']}: 미완료 또는 허용되지 않은 점수")
    return errors


def score(j: dict, rb: dict) -> dict:
    errors = judgment_errors(j, rb)
    content_max = sum(d["points"] - d.get("discretion", {}).get("points", 0) for d in rb["documents"])
    result = {"complete": not errors, "provisional": bool(j.get("provisional")), "errors": errors, "missing": len(errors),
              "content": None, "content_max": content_max, "discretion": None,
              "total": None, "total_max": rb["meta"]["total"], "docs": {}}
    if errors:
        return result  # 미완료는 0점이 아니다. 총점·부분 합계를 확정하지 않는다.
    for doc in rb["documents"]:
        disc_max = doc.get("discretion", {}).get("points", 0)
        cap = doc["points"] - disc_max
        regular = bonus = 0.0
        per_issue = {}
        for issue in doc["issues"]:
            rows = {r["id"]: r for r in j["issues"][issue["id"]]["items"]}
            got = 0.0
            for item in rubric.items(issue):
                awarded = rows[item["id"]]["awarded"]
                if item.get("type") == "bonus":
                    bonus += awarded
                else:
                    regular += awarded
                    got += awarded
            per_issue[issue["id"]] = got
        discretion = j["discretion"][doc["id"]]["awarded"] if disc_max else 0
        result["docs"][doc["id"]] = {
            "content": min(regular + bonus, cap), "content_max": cap,
            "discretion": discretion, "discretion_max": disc_max, "issues": per_issue}
    result["content"] = sum(d["content"] for d in result["docs"].values())
    result["discretion"] = sum(d["discretion"] for d in result["docs"].values())
    result["total"] = result["content"] + result["discretion"]
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="채점 결과 합산")
    ap.add_argument("--judge", required=True, help="채점 모델 id")
    ap.add_argument("-t", "--tasks", nargs="*", help="과제 id (기본: 채점 결과가 있는 모든 과제)")
    ap.add_argument("--results", type=Path, default=common.RESULTS)
    common.add_data_argument(ap)
    ap.add_argument("-m", "--models", nargs="+", help="이 답안만 집계; 누락된 대상은 오류")
    ap.add_argument("--source", choices=("saved", "current"), default="saved",
                    help="saved: 당시 입력으로 재집계, current: 현재 자료도 대조")
    ap.add_argument("--detail", action="store_true", help="공소사실(쟁점)별 점수 출력")
    ap.add_argument("--csv", type=Path, help="점수표 CSV 저장 경로")
    args = ap.parse_args()

    base = args.results / "judgments" / common.slug(args.judge)
    expected = runs.expected_jobs(args.results, args.judge)
    tasks = args.tasks or sorted({p.name for p in base.glob("*") if p.is_dir()} | {t for t, _ in expected})
    rows, seen, conditions = [], set(), {}
    incomplete = 0
    for task in tasks:
        files = sorted((base / task).glob("*.json"))
        if not files:
            incomplete += 1
            print(f"[score] 채점 결과 없음: {task}", file=sys.stderr)
        for f in files:
            try:
                j = common.read_json(f)
                if args.models and j["model"] not in args.models:
                    continue
                if j.get("judge") != args.judge or f.name != common.slug(j["model"]) + ".json":
                    raise ValueError("채점 모델 또는 답안 파일 이름 불일치")
                seen.add((task, j["model"]))
                rb = saved_rubric(j, task) if args.source == "saved" else common.load_rubric(task, args.data_root)
                errors = rubric.validate(rb)
                if args.source == "current":
                    errors += source_errors(j, rb, args.data_root, args.results)
                s = score(j, rb)
                errors += s["errors"]
                condition = {k: j.get(k) for k in ("rubric_sha", "record_sha", "prompt_template_sha", "params", "protocol")}
                condition["source_hashes"] = grading.source_hashes(j.get("input_sources", {}))
                if task in conditions and conditions[task] != condition:
                    errors.append("같은 과제에 다른 기준·실행 설정의 결과가 섞여 있음")
                if errors:
                    raise ValueError("; ".join(errors))
                conditions[task] = condition
                rows.append((task, j["model"], s, rb))
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                incomplete += 1
                print(f"[score] 유효하지 않은 결과 제외: {f} — {error}", file=sys.stderr)
    if args.models:
        expected.update((task, model) for task in tasks for model in args.models)
    missing = {(t, m) for t, m in expected if t in tasks and (not args.models or m in args.models)} - seen
    for task, model in sorted(missing):
        incomplete += 1
        print(f"[score] 예정된 결과 누락: {task} × {model}", file=sys.stderr)
    if not rows:
        sys.exit(f"[score] 집계할 수 있는 완료된 채점 결과 없음: {base}")

    print(f"## 점수표 (채점: {args.judge})\n")
    if incomplete:
        print(f"미완료 또는 검증 실패 결과 {incomplete}건 제외.\n")
    print("| 과제 | 모델 | 상태 | 내용 점수(재량 제외) | 재량 | 총점 | 문서별 내용 점수 |")
    print("|---|---|---|---|---|---|---|")
    for task, model, s, rb in rows:
        per_doc = " · ".join(f"{d} {v['content']:g}/{v['content_max']:g}" for d, v in s["docs"].items())
        state = "잠정(루브릭 미확정)" if s["provisional"] else "채점 완료"
        print(f"| {task} | {model} | {state} | {s['content']:g}/{s['content_max']:g} | {s['discretion']:g} | "
              f"{s['total']:g}/{s['total_max']:g} | {per_doc} |")

    if args.detail:
        for task in dict.fromkeys(r[0] for r in rows):
            trows = [r for r in rows if r[0] == task]
            rb = trows[0][3]
            print(f"\n### {task} — 공소사실(쟁점)별\n")
            print("| 쟁점 | 배점 | " + " | ".join(r[1] for r in trows) + " |")
            print("|---|---|" + "---|" * len(trows))
            for doc, issue in rubric.issues(rb):
                cells = " | ".join(f"{r[2]['docs'][doc['id']]['issues'][issue['id']]:g}" for r in trows)
                print(f"| {issue['id']} {issue['title']} | {issue['points']:g} | {cells} |")
            print("\n※ 쟁점별 표는 일반 항목 점수다. 별도 가점은 문서별 배점 상한 안에서 내용 점수에 합산한다.")

    if args.csv:
        if incomplete:
            sys.exit("[score] 미완료·누락·조건 불일치가 있어 CSV를 저장하지 않음")
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["task", "model", "judge", "content", "content_max", "discretion", "total", "total_max",
                        "missing", "provisional"])
            for task, model, s, _ in rows:
                w.writerow([task, model, args.judge, s["content"], s["content_max"], s["discretion"],
                            s["total"], s["total_max"], s["missing"], s["provisional"]])
        print(f"\n[score] CSV 저장: {args.csv}", file=sys.stderr)

    if incomplete:
        sys.exit(1)


if __name__ == "__main__":
    common.cli(main)
