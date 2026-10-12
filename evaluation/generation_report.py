#!/usr/bin/env python3
"""예정된 전체 과제를 분모로 답안 생성 완료율과 누적 자원 사용을 집계한다."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

import common
import generate


def planned_jobs(results):
    jobs = set()
    for path in (results / "runs").glob("*-generate.json"):
        run = common.read_json(path)
        jobs.update((job["task"], job["model"]) for job in run["jobs"])
    return jobs


def task_result(results, task, model):
    path = common.answer_file(results, task, model)
    if not path.exists():
        path = path.with_name(path.stem + ".failed.json")
    row = {"task": task, "model": model, "outcome": "missing", "attempts": 0,
           "first_attempt_complete": False, "completed": False, "ready_for_grading": False,
           "selected_attempt": None, "configuration_status": "unchecked", "total_cost_usd": None,
           "total_elapsed_s": None, "total_usage": {}}
    if not path.exists():
        return row, []
    result = common.read_json(path)
    if result.get("schema") != generate.SCHEMA:
        raise ValueError(f"현행 생성 정책으로 실행한 결과가 아님: {path}")
    if (result.get("task"), result.get("model")) != (task, model):
        raise ValueError(f"답안의 과제·모델 ID 불일치: {path}")
    attempts = generate.attempt_history(result)
    if not 1 <= len(attempts) <= result["policy"]["max_attempts"]:
        raise ValueError(f"생성 시도 상한 위반: {path}")
    for number, attempt in enumerate(attempts, 1):
        if attempt["attempt"] != number:
            raise ValueError(f"시도 번호가 연속되지 않음: {path}")
        if number < len(attempts) and attempt["outcome"] not in generate.RETRYABLE:
            raise ValueError(f"허용되지 않은 재시도: {path}")
        for key in ("params", "policy", "prompt_sha", "request"):
            if attempt[key] != result[key]:
                raise ValueError(f"재시도 설정이 다름 ({key}): {path}")
    completed = result["outcome"] == "completed"
    if result["selected_attempt"] != (len(attempts) if completed else None):
        raise ValueError(f"최초 완료 답안 선택이 일치하지 않음: {path}")
    if completed and generate.outcome(result) != "completed":
        raise ValueError(f"완료 상태와 응답이 일치하지 않음: {path}")
    row.update(outcome=result["outcome"], attempts=len(attempts),
               first_attempt_complete=attempts[0]["outcome"] == "completed", completed=completed,
               ready_for_grading=completed and not result.get("error"),
               selected_attempt=result["selected_attempt"], configuration_status=result["configuration_status"],
               **generate.totals(attempts))
    return row, attempts


def build_report(results, tasks=None, models=None):
    jobs = {(t, m) for t in tasks for m in models} if tasks and models else planned_jobs(results)
    if not jobs:
        raise ValueError("예정된 생성 과제가 없음. --tasks와 --models로 전체 대상 지정 가능")
    rows, attempts_by_model = [], {}
    for task, model in sorted(jobs):
        row, attempts = task_result(results, task, model)
        rows.append(row)
        attempts_by_model.setdefault(model, []).extend(attempts)
    summaries = {}
    for model, attempts in attempts_by_model.items():
        selected = [row for row in rows if row["model"] == model]
        settings = {json.dumps({k: a[k] for k in ("params", "policy", "prompt_template_sha")}, sort_keys=True)
                    for a in attempts}
        if len(settings) > 1:
            raise ValueError(f"과제 사이에 생성 설정이 다름: {model}")
        planned = len(selected)
        first = sum(row["first_attempt_complete"] for row in selected)
        completed = sum(row["completed"] for row in selected)
        unknown = sum(a.get("cost_usd") is None for a in attempts)
        missing = [row["task"] for row in selected if row["outcome"] == "missing"]
        resources = generate.totals(attempts)
        if missing:
            resources.update(total_cost_usd=None, total_elapsed_s=None,
                             total_usage={key: None for key in resources["total_usage"]})
        summaries[model] = {
            "planned": planned, "first_attempt_completed": first, "completed": completed,
            "first_attempt_completion_rate": first / planned, "completion_rate": completed / planned,
            "ready_for_grading": sum(row["ready_for_grading"] for row in selected),
            "completed_tasks": [row["task"] for row in selected if row["completed"]],
            "incomplete_tasks": [row["task"] for row in selected if not row["completed"]],
            "missing_tasks": missing,
            "attempts": len(attempts), "attempt_outcomes": dict(Counter(a["outcome"] for a in attempts)),
            "known_cost_usd": sum(a.get("cost_usd") or 0 for a in attempts),
            "unknown_cost_attempts": unknown, **resources,
        }
    return {"schema": "generation-report-v1", "models": summaries, "tasks": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=common.RESULTS)
    parser.add_argument("-t", "--tasks", nargs="+", help="아직 실행하지 않은 과제를 포함한 전체 대상")
    parser.add_argument("-m", "--models", nargs="+", help="예정된 생성 모델; --tasks와 함께 지정")
    parser.add_argument("--json", type=Path, help="상세 집계 JSON 저장 위치")
    parser.add_argument("--csv", type=Path, help="과제별 집계 CSV 저장 위치")
    args = parser.parse_args()
    if bool(args.tasks) != bool(args.models):
        parser.error("--tasks와 --models는 함께 지정해야 함")
    if args.tasks:
        for task in args.tasks:
            common.parse_task(task)
        common.unique_names(args.tasks, "과제")
        common.unique_names(args.models, "모델")
    report = build_report(args.results, args.tasks, args.models)
    for model, row in report["models"].items():
        print(f"{model}: 첫 시도 {row['first_attempt_completed']}/{row['planned']} · "
              f"시도 한도 내 완료 {row['completed']}/{row['planned']} · 채점 가능 {row['ready_for_grading']} · "
              f"누적 비용(USD) {row['total_cost_usd'] if row['total_cost_usd'] is not None else '미확인 포함'}")
    if args.json:
        common.write_json(args.json, report)
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        fields = [key for key in report["tasks"][0] if key != "total_usage"]
        with args.csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(report["tasks"])
    return 0


if __name__ == "__main__":
    common.cli(main)
