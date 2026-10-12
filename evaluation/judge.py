#!/usr/bin/env python3
"""과목별 또는 항목별 채점, 응답 검증, 결과·실행 이력 저장."""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import grading  # noqa: E402
import prompts  # noqa: E402
import rubric  # noqa: E402
import llm
import runs
import score
from llm import LLMError, chat, load_api_key, load_prices, summarize  # noqa: E402

KIND = {"disposition": "결론", "bonus": "가점"}
OUT_TOKENS_PER_ITEM = 600  # 예상 비용 계산용 (추론 토큰은 별도이며 실제 사용량에 따라 달라짐)
PROFILES = {
    "gpt-6-astra": {"judge": "gpt-6-astra", "api_format": "responses",
                    "reasoning_effort": "high", "max_tokens": 128000},
}


def apply_profile(args):
    """본실험 채점 설정을 적용하고 상충하는 명령행 인자는 거부한다."""
    if not args.profile:
        return
    for key, value in PROFILES[args.profile].items():
        supplied = getattr(args, key)
        if supplied is not None and supplied != value:
            raise ValueError(f"--profile과 --{key.replace('_', '-')} 설정이 다름")
        setattr(args, key, value)


def format_item(item: dict) -> str:
    """YAML의 기준·해설·점수표를 표시한다. basis·note·flag는 검토자용이다."""
    lines = [f"- id: {item['id']} | 배점: {item['points']:g}점 | 유형: {KIND.get(item.get('type'), '일반')}",
             f"  기준: {item['criterion']}", "  점수표:"]
    lines.extend(f"    {line}" for line in rubric.scoring_text(item).splitlines())
    for key, label in (("accept", item.get("accept_label", "함께 인정")), ("record", "기록 근거")):
        if item.get(key):
            lines.append(f"  {label}: {item[key]}")
    guidance = rubric.explanation_text(item)
    if guidance:
        lines.extend(f"  {line}" for line in guidance.splitlines())
    return "\n".join(lines)


def item_messages(rb: dict, doc: dict, issue_title: str, item: dict,
                  answer: str, record: str) -> list[dict]:
    rules = "\n".join(f"{n}. {rule}" for n, rule in enumerate(rb["meta"]["grading_rules"], 1))
    system = prompts.JUDGE_SYSTEM.format(subject=rb["meta"]["subject"],
                                        exam_month=common.exam_month(rb["meta"]["id"]), grading_rules=rules)
    user = prompts.JUDGE_USER.format(doc_title=doc["title"], issue_title=issue_title,
                                     item=format_item(item), answer=answer, record=record)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_json(text: str) -> dict:
    """JSON 또는 JSON 코드 블록만 읽는다. 내용 평가에는 사용하지 않는다."""
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    return json.loads(text, parse_constant=common.reject_constant)


def empty_item(item: dict) -> dict:
    return {"id": item["id"], "max": item["points"], "awarded": None,
            "quotes": [], "record_lines": [], "reason": "채점 미완료"}


def check_item(data: dict | None, item: dict, answer: str, record: str) -> tuple[dict, list[str]]:
    """응답 형식·허용 점수·인용의 존재만 확인한다. 법률적 의미와 점수는 LLM이 판단한다."""
    row = empty_item(item)
    fields = {"id", "awarded", "quotes", "record_lines", "reason"}
    if not isinstance(data, dict) or set(data) != fields:
        return row, ["항목 하나의 id·awarded·quotes·record_lines·reason이 필요함"]
    problems = []
    if data["id"] != item["id"]:
        problems.append(f"응답 id 불일치: {data['id']!r}")
    if not rubric.valid_score(data["awarded"], item):
        problems.append(f"허용되지 않은 점수: {data['awarded']!r}")
    if not isinstance(data["reason"], str) or not data["reason"].strip():
        problems.append("판단 이유가 없음")
    quotes = data["quotes"]
    if not isinstance(quotes, list):
        problems.append("quotes는 답안 원문 인용의 배열이어야 함")
    else:
        # 공백·줄바꿈 차이만 허용한다. 인용이 점수를 뒷받침하는지는 LLM이 판단한다.
        source = "".join(answer.split())
        for quote in quotes:
            if not isinstance(quote, str) or not quote.strip() or "".join(quote.split()) not in source:
                problems.append("답안 원문에 없는 인용이 있음")
    lines = data["record_lines"]
    line_count = len(record.splitlines())
    if not isinstance(lines, list) or any(type(n) is not int or not 1 <= n <= line_count for n in lines):
        problems.append("record_lines는 기록에 존재하는 줄 번호의 배열이어야 함")
    if rubric.valid_score(data["awarded"], item) and data["awarded"] > 0 and not data["quotes"]:
        problems.append("양의 점수에 답안 인용이 없음")
    if not problems:
        row.update(data)
    return row, problems


def run_call(key, msgs, judge, prices, temperature, effort, max_tokens=None, timeout=900,
             base_url=llm.BASE_URL, seed=None, extra_body=None, api_format="chat", token_limit_field="max_tokens"):
    """한 요청을 실행하고 실패 시에도 수신한 내용·사용량을 보존한다."""
    try:
        resp = chat(key, msgs, judge, max_tokens=max_tokens, temperature=temperature,
                    reasoning_effort=effort, stream=True, retries=0, timeout=timeout,
                    base_url=base_url, seed=seed, extra_body=extra_body, api_format=api_format,
                    token_limit_field=token_limit_field)
        summary = summarize(resp, prices, requested=judge)
    except LLMError as error:
        try:
            partial = summarize(error.response, prices, requested=judge)
        except LLMError:
            partial = {"cost_usd": None}
        return None, partial, str(error)
    if summary["finish_reason"] != "stop":
        return None, summary, f"응답 미완료: {summary['finish_reason']}"
    try:
        return parse_json(summary["content"]), summary, ""
    except ValueError as error:
        return None, summary, f"JSON 파싱 실패: {error}"


def failed_calls(j: dict, rb: dict) -> set[tuple[str, str]]:
    """실패·누락된 세부 항목만 다시 호출한다. 저장되지 못한 항목·문서도 포함한다."""
    out = set()
    for _, issue in rubric.issues(rb):
        rows = {r["id"]: r for r in j.get("issues", {}).get(issue["id"], {}).get("items", [])}
        for item in rubric.items(issue):
            row = rows.get(item["id"], {})
            if row.get("error") or row.get("awarded") is None:
                out.add(("item", item["id"]))
    for doc in rb["documents"]:
        row = j.get("discretion", {}).get(doc["id"], {})
        if doc.get("discretion") and (row.get("error") or row.get("awarded") is None):
            out.add(("discretion", doc["id"]))
    return out


def calls_for(rb, unit, answer, record, context):
    if unit == "task":
        return [("task", rb["meta"]["id"], None, grading.messages(rb, answer, record, context, format_item))]
    calls = [("item", item["id"], item, item_messages(rb, doc, issue["title"], item, answer, record))
             for doc, issue in rubric.issues(rb) for item in rubric.items(issue)]
    calls += [("discretion", doc["id"], doc["discretion"],
               item_messages(rb, doc, "문서 재량", doc["discretion"], answer, record))
              for doc in rb["documents"] if doc.get("discretion")]
    return calls


def request(key, messages, args, prices):
    return run_call(key, messages, args.judge, prices, args.temperature, args.reasoning_effort,
                    args.max_tokens, args.timeout, args.base_url, args.seed, args.extra_body, args.api_format,
                    args.token_limit_field)


def grade_task(result, previous, call, rb, answer, record, args, prices, key, out):
    prior_cost = result["cost_usd"]
    history = list(previous.get("previous_attempts", [])) if previous else []
    if previous:
        history.append({k: previous.get(k) for k in ("call", "issues", "discretion", "validation_errors", "totals")})
    result.update(previous_attempts=history, call={"error": "요청 미완료", "attempt": len(history) + 1},
                  **grading.check_response(None, rb, answer, record, check_item))
    result["cost_usd"] = None
    common.write_json(out, result)
    start = time.monotonic()
    data, summary, error = request(key, call[3], args, prices)
    result.update(grading.check_response(data, rb, answer, record, check_item))
    result["call"] = {"at": runs.now(), "attempt": len(history) + 1, "error": error or None,
                      "elapsed_s": round(time.monotonic() - start, 3), **summary}
    result["cost_usd"] = common.add_cost(prior_cost, summary.get("cost_usd"))


def grade_items(result, calls, rb, answer, record, args, prices, key, out):
    issues = {it["id"]: issue for _, issue in rubric.issues(rb) for it in rubric.items(issue)}
    cost = result["cost_usd"]
    result["cost_usd"] = None
    common.write_json(out, result)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        responses = executor.map(lambda c: request(key, c[3], args, prices), calls)
        for (kind, cid, item, messages), (data, summary, error) in zip(calls, responses):
            row, problems = check_item(data, item, answer, record)
            if error:
                problems.append(error)
            row.update(usage=summary.get("usage"), finish_reason=summary.get("finish_reason"),
                       served_model=summary.get("served_model"), attempts=1, cost_usd=summary.get("cost_usd"),
                       prompt_sha=common.sha(json.dumps(messages, ensure_ascii=False)),
                       raw_response=summary.get("content", ""), error="; ".join(problems) or None)
            cost = common.add_cost(cost, summary.get("cost_usd"))
            if kind == "item":
                issue = issues[cid]
                group = result["issues"].setdefault(issue["id"], {"items": []})
                rows = {r["id"]: r for r in group["items"]}
                old = rows.get(cid, {})
                rows[cid] = row
                group["items"] = [rows.get(it["id"], empty_item(it)) for it in rubric.items(issue)]
            else:
                old = result["discretion"].get(cid, {})
                result["discretion"][cid] = row
            if old.get("raw_response") or old.get("error"):
                row["previous_attempts"] = old.get("previous_attempts", []) + [
                    {k: v for k, v in old.items() if k != "previous_attempts"}]
            common.write_json(out, result)
    result["cost_usd"] = cost


def prepare_task(args, task):
    raw_rubric = common.rubric_file(task, args.data_root).read_text(encoding="utf-8")
    rb = common.parse_rubric(raw_rubric, task)
    errors = rubric.validate(rb)
    if errors:
        raise ValueError("루브릭 오류: " + "; ".join(errors))
    unit = args.grading_unit
    if unit == "auto":
        unit = "task" if rb["meta"].get("reference_guide") else "item"
    if unit == "item" and rb["meta"].get("reference_guide"):
        raise ValueError("기준답안이 연결된 과제는 --grading-unit task를 사용해야 함")
    context = grading.load_context(rb, args.data_root) if unit == "task" else None
    record = common.load_record(task, numbered=True, root=args.data_root)
    return raw_rubric, rb, unit, context, record


def grade_answer(args, task, label, run, prices, prepared):
    raw_rubric, rb, unit, context, record = prepared
    source = args.answer_file or (args.answers or args.results / "answers") / task / f"{common.slug(label)}.json"
    if source.suffix == ".json":
        candidate = common.read_json(source)
        if candidate.get("task", task) != task or (not args.answer_file and candidate.get("model", label) != label):
            raise ValueError(f"답안의 과제·모델 식별자가 요청과 다름: {source}")
    answer = common.answer_text(source)
    if not answer.strip():
        raise ValueError(f"빈 답안: {source}")
    calls = calls_for(rb, unit, answer, record, context)
    estimates = [llm.check_budget(c[3], args) for c in calls]
    messages = calls[0][3] if unit == "task" else [c[3] for c in calls]
    fingerprint = {"rubric_sha": common.sha(raw_rubric), "record_sha": common.sha(record),
                   "answer_sha": common.sha(answer), "prompt_sha": common.sha(json.dumps(messages, ensure_ascii=False)),
                   "prompt_template_sha": grading.template_sha() if unit == "task" else common.sha(prompts.JUDGE_SYSTEM + prompts.JUDGE_USER)}
    sources = context["sources"] if context else {}
    protocol = common.TASK_JUDGING_PROTOCOL if unit == "task" else common.JUDGING_PROTOCOL
    params = {**llm.parameters(args), "repeat": args.repeat}
    out = common.judgment_file(args.results, task, label, args.judge)
    previous = common.read_json(out) if out.exists() and not args.force and not (args.dry_run and not args.retry_failed) else None
    if previous:
        if any(previous.get(k) != v for k, v in {"task": task, "model": label, "judge": args.judge,
                                                 "protocol": protocol, "grading_unit": unit}.items()):
            raise ValueError("이전 채점 방식 또는 다른 과제·모델의 결과. 새 --results 폴더를 사용할 것")
        if (any(previous.get(k) != v for k, v in fingerprint.items()) or previous.get("params") != params
                or grading.source_hashes(previous.get("input_sources", {})) != grading.source_hashes(sources)):
            raise ValueError("루브릭·기록·답안·기준답안·규칙·프롬프트·설정 변경. 새 --results 폴더를 사용할 것")
        complete = score.score(previous, rb)["complete"]
        if complete or not args.retry_failed:
            run.record(task, label, "skipped" if complete else "failed", result=str(out),
                       error=None if complete else "미완료 결과: --retry-failed 필요")
            return 0, 0, 0
        if unit == "item":
            todo = failed_calls(previous, rb)
            calls = [c for c in calls if (c[0], c[1]) in todo]
            estimates = [llm.check_budget(c[3], args) for c in calls]
    if args.dry_run:
        if args.dump:
            folder = args.dump / task / common.slug(label)
            folder.mkdir(parents=True, exist_ok=True)
            for kind, cid, _, msgs in calls:
                (folder / f"{kind}-{cid}.txt").write_text(
                    "\n\n".join(f"### {m['role']}\n{m['content']}" for m in msgs), encoding="utf-8")
                common.write_json(folder / f"{kind}-{cid}.json", {"messages": msgs, **fingerprint, "input_sources": sources})
        run.record(task, label, "preview")
        count = sum(1 for _ in grading.entries(rb)) if unit == "task" else len(calls)
        print(f"[judge] {task} × {label}: {len(calls)}회 · 입력≈{sum(estimates):,}토큰", file=sys.stderr)
        return len(calls), sum(estimates), OUT_TOKENS_PER_ITEM * count
    key = load_api_key(args.api_key_env, args.api_key_file)
    snapshot = {"rubric": raw_rubric, "record": record, "answer": answer,
                "sources": context.get("source_texts", {}) if context else {}}
    result = previous if unit == "item" and previous else {
        "task": task, "model": label, "judge": args.judge, "created": runs.now(),
        "protocol": protocol, "grading_unit": unit, **fingerprint, "params": params,
        "rubric_version": rb["meta"]["version"], "rubric_status": rb["meta"].get("status"),
        "provisional": grading.is_provisional(rb), "answer_source": str(source.resolve()),
        "input_sources": sources, "input_snapshot": snapshot, "messages": messages,
        "issues": {issue["id"]: {"items": [empty_item(it) for it in rubric.items(issue)]}
                   for _, issue in rubric.issues(rb)}, "discretion": {},
        "cost_usd": previous.get("cost_usd") if previous else 0.0}
    result["run_id"] = run.id
    if unit == "task":
        grade_task(result, previous, calls[0], rb, answer, record, args, prices, key, out)
    else:
        if previous:
            result.setdefault("retried", []).append({"at": runs.now(), "calls": [c[1] for c in calls], "params": params})
        grade_items(result, calls, rb, answer, record, args, prices, key, out)
    result["totals"] = score.score(result, rb)
    common.write_json(out, result)
    complete = result["totals"]["complete"]
    run.record(task, label, "completed" if complete else "failed", result=str(out),
               error=None if complete else "; ".join(result["totals"]["errors"]))
    print(f"[judge] {task} × {label}: {'완료' if complete else '미완료'} · {out}", file=sys.stderr)
    return 0, 0, 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-t", "--tasks", nargs="+", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("-m", "--models", nargs="+", help="채점할 답안의 모델 ID")
    source.add_argument("--answer-file", type=Path, help="평가할 답안 텍스트 또는 생성 결과 JSON")
    parser.add_argument("--name", default="reference", help="--answer-file의 답안 식별자")
    parser.add_argument("--judge", required=True, help="채점 모델 ID")
    parser.add_argument("--results", type=Path, default=common.RESULTS, help="실험 결과 폴더")
    parser.add_argument("--answers", type=Path, help="기존 answers 폴더")
    parser.add_argument("--repeat", type=common.positive_int, default=1, help="반복 실험 번호; 반복마다 별도 --results 사용")
    common.add_data_argument(parser)
    llm.add_arguments(parser)
    parser.set_defaults(api_format=None)
    parser.add_argument("--profile", choices=PROFILES,
                        help="본실험 채점 설정: Responses API·high·출력 상한 128,000토큰")
    parser.add_argument("--grading-unit", choices=("auto", "task", "item"), default="auto")
    parser.add_argument("--workers", type=common.positive_int, default=4, help="항목별 모드의 동시 요청 수")
    retry = parser.add_mutually_exclusive_group()
    retry.add_argument("--retry-failed", action="store_true", help="같은 조건의 미완료 채점 재시도")
    retry.add_argument("--force", action="store_true", help="기존 결과 덮어쓰기; 이력 보존에는 새 결과 폴더 사용")
    parser.add_argument("--dry-run", action="store_true", help="API 없이 입력·요청 수 확인")
    parser.add_argument("--dump", type=Path, help="--dry-run 프롬프트 저장 폴더")
    args = parser.parse_args()
    labels = args.models or [args.name]
    try:
        apply_profile(args)
        args.api_format = args.api_format or "chat"
        llm.validate_arguments(args)
        common.unique_names(labels, "답안")
        common.unique_names(args.tasks, "과제")
        common.unique_names([args.judge], "채점 모델")
        for task in args.tasks:
            common.parse_task(task)
        if args.dump and not args.dry_run:
            raise ValueError("--dump는 --dry-run과 함께 사용해야 함")
    except ValueError as error:
        parser.error(str(error))
    jobs = [{"task": t, "model": m} for t in args.tasks for m in labels]
    estimates = [0, 0, 0]
    with runs.Run(args, "judge", jobs) as run:
        prices = load_prices(args.prices, args.base_url)
        prepared = {}
        for job in jobs:
            try:
                if job["task"] not in prepared:
                    prepared[job["task"]] = prepare_task(args, job["task"])
                counts = grade_answer(args, job["task"], job["model"], run, prices, prepared[job["task"]])
                estimates = [a + b for a, b in zip(estimates, counts)]
            except (OSError, ValueError, KeyError, TypeError, LLMError) as error:
                run.record(**job, status="failed", error=str(error))
                print(f"[judge] {job['task']} × {job['model']}: {error}", file=sys.stderr)
    if args.dry_run:
        price = prices.get(args.judge)
        cost = f"${estimates[1] * price[0] + estimates[2] * price[1]:.2f}" if price else "알 수 없음"
        print(f"채점 호출 {estimates[0]}건 · 입력≈{estimates[1]:,}토큰 · 출력≈{estimates[2]:,}토큰 · 예상 비용 {cost}")
    else:
        print(f"[judge] 실행 기록: {run.path}", file=sys.stderr)
    return 1 if run.failed else 0


if __name__ == "__main__":
    common.cli(main)
