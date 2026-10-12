#!/usr/bin/env python3
"""같은 입력·설정으로 제한 재시도하여 최초로 완료된 기록형 답안을 보존한다."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
import llm
import prompts
import runs
from llm import LLMError, chat, load_api_key, load_prices, summarize

PROTOCOL = "record-generation-v2"
SCHEMA = "generation-result-v3"
RETRYABLE = {"length", "empty"}
PROFILES = {
    "glm-5.3": {"reasoning_effort": "high", "max_tokens": 131072, "token_limit_field": "max_tokens"},
    "deepseek-v4.1-flash": {"reasoning_effort": "high", "max_tokens": 393216, "token_limit_field": "max_tokens"},
    "mimo-v2.6-pro": {"reasoning_effort": None, "max_tokens": 131072, "token_limit_field": "max_completion_tokens"},
    "qwen3.8-max": {"reasoning_effort": "medium", "max_tokens": 131072, "token_limit_field": "max_completion_tokens"},
}


def apply_profile(args):
    """본실험 설정을 적용한다. 공급사와 실제 API 모델 ID는 명령행에서 지정한다."""
    if not args.profile:
        return
    if len(args.models) != 1 or args.api_format != "chat" or args.max_attempts != 3:
        raise ValueError("--profile은 모델 ID 하나·Chat API·총 3회 시도와 함께 사용해야 함")
    for key, value in PROFILES[args.profile].items():
        supplied = getattr(args, key)
        if supplied is not None and supplied != value:
            raise ValueError(f"--profile과 --{key.replace('_', '-')} 설정이 다름")
        setattr(args, key, value)
    args.require_reasoning = True


def check_profile_body(args):
    if not args.profile:
        return
    body = args.extra_body or {}
    if (body.get("enable_thinking") is False or "thinking_budget" in body
            or "thinking" in body and body["thinking"] != {"type": "enabled"}):
        raise ValueError("--profile의 추론 설정을 --extra-body로 바꿀 수 없음")
    if args.profile == "mimo-v2.6-pro":
        if body.get("reasoning", {}).get("effort") is not None:
            raise ValueError("MiMo 본실험은 reasoning effort 없이 thinking enabled로 고정")
        args.extra_body = {**body, "thinking": {"type": "enabled"}}


def condition(args):
    return {"schema": SCHEMA, "protocol": PROTOCOL, "prompt_template_sha": common.sha(prompts.GENERATE_SYSTEM),
            "params": {**llm.parameters(args), "repeat": args.repeat, "require_reasoning": args.require_reasoning},
            "policy": {"selection": "first-completed-v1", "max_attempts": args.max_attempts, "technical_retries": 0}}


def check_conditions(args, expected):
    """같은 실험의 다른 과목에도 모델별 동일 설정을 적용한다."""
    for path in (args.results / "runs").glob("*-generate.json"):
        prior = common.read_json(path)
        if set(args.models).intersection(job["model"] for job in prior["jobs"]):
            if prior.get("generation_condition") != expected:
                raise ValueError("같은 실험의 생성 설정이 다름. 새 --results 폴더를 사용할 것")


def build_messages(task, root=None):
    return [{"role": "system", "content": prompts.GENERATE_SYSTEM.format(exam_month=common.exam_month(task))},
            {"role": "user", "content": common.load_record(task, root=root)}]


def reasoning_evidence(summary):
    """응답에 관찰된 추론을 기록한다. 토큰 수로 추론 강도를 추정하지 않는다."""
    details = (summary.get("usage") or {}).get("completion_tokens_details") or {}
    tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if type(tokens) is not int or tokens < 0:
        tokens = None
    text = summary.get("reasoning") or ""
    chars = len(text.strip()) if isinstance(text, str) else 0
    return {"observed": chars > 0 or (tokens is not None and tokens > 0),
            "text_characters": chars, "reported_tokens": tokens}


def outcome(summary):
    """API 상태와 본문 유무만 판정한다. 답안 내용의 충실성은 채점 대상이다."""
    finish = summary.get("finish_reason")
    if summary.get("refusal") or finish == "content_filter":
        return "refusal"
    if finish == "length":
        return "length"
    if finish != "stop":
        return "unexpected_finish"
    return "completed" if summary.get("content", "").strip() else "empty"


def attempt_history(result):
    return [*result.get("previous_attempts", []),
            {k: v for k, v in result.items() if k != "previous_attempts"}]


def totals(attempts):
    """불명확한 사용량·비용은 0으로 대체하지 않는다."""
    def total(values):
        return None if any(v is None for v in values) else sum(values)

    return {"total_cost_usd": total([a.get("cost_usd") for a in attempts]),
            "total_elapsed_s": total([a.get("elapsed_s") for a in attempts]),
            "total_usage": {key: total([(a.get("usage") or {}).get(key) for a in attempts])
                            for key in ("prompt_tokens", "completion_tokens", "total_tokens")}}


def record_result(run, task, model, result, path, *, reused=False):
    ready = result["outcome"] == "completed" and not result.get("error")
    status = ("skipped" if reused else "completed") if ready else "failed"
    run.record(task, model, status,
               result=str(path), outcome=result["outcome"], attempts=result["attempt"],
               selected_attempt=result["selected_attempt"], error=result.get("error"))


def generate_one(args, task, model, run, prices, messages):
    tokens = llm.check_budget(messages, args)
    out = common.answer_file(args.results, task, model)
    failed = out.with_name(out.stem + ".failed.json")
    fingerprint = {"task": task, "model": model, **condition(args),
                   "prompt_sha": common.sha(json.dumps(messages, ensure_ascii=False))}
    previous_path = out if out.exists() else failed
    previous = common.read_json(previous_path) if previous_path.exists() else None
    if previous:
        if any(previous.get(k) != v for k, v in fingerprint.items()):
            raise ValueError("기록·프롬프트·모델·설정이 다른 기존 답안. 새 --results 폴더를 사용할 것")
        can_retry = previous["outcome"] in RETRYABLE and previous["attempt"] < args.max_attempts
        if not can_retry or not args.retry_failed:
            record_result(run, task, model, previous, previous_path, reused=True)
            if previous.get("error"):
                print(f"[generate] {task} × {model}: {previous['error']} "
                      f"(저장된 시도 {previous['attempt']}/{args.max_attempts})", file=sys.stderr)
            return 0, 0.0
    if args.dry_run:
        price = prices.get(model)
        remaining = args.max_attempts - (previous["attempt"] if previous else 0)
        cost = (tokens * price[0] + args.assume_output * price[1]) * remaining if price else None
        print(f"{task} × {model}: 입력≈{tokens:,}토큰 · 최대 {remaining}회 · "
              f"예상 비용 {cost if cost is not None else '알 수 없음'}")
        request = llm.build_request(messages, model, **llm.parameters(args))
        settings = {k: v for k, v in request["body"].items() if k not in {"messages", "input"}}
        print(f"  {request['url']} · {json.dumps(settings, ensure_ascii=False)}")
        run.record(task, model, "preview")
        return remaining, cost
    key = load_api_key(args.api_key_env, args.api_key_file)
    history = attempt_history(previous) if previous else []
    count, spent = 0, 0.0
    while len(history) < args.max_attempts:
        number = len(history) + 1
        raw_path = out.parent / (out.stem + ".attempts") / f"{number:02d}.response.txt"
        result = {**fingerprint, "created": runs.now(), "run_id": run.id, "messages": messages,
                  "request": llm.build_request(messages, model, **llm.parameters(args)),
                  "raw_response_file": str(raw_path.relative_to(args.results)),
                  "previous_attempts": history, "attempt": number, "selected_attempt": None,
                  "outcome": "interrupted", "configuration_status": "unchecked",
                  "error": "생성 완료 상태 미확인", "content": "", "finish_reason": None,
                  "cost_usd": None, "elapsed_s": None}
        result.update(totals([*history, result]))
        common.write_json(failed, result)
        start = time.monotonic()
        try:
            response = chat(key, messages, model, raw_response_path=raw_path, **llm.parameters(args))
            summary = summarize(response, prices, model)
            status = outcome(summary)
            error = None if status == "completed" else f"생성 미완료: {status}"
        except LLMError as exc:
            try:
                summary = summarize(exc.response, prices, model)
            except LLMError:
                summary = {"content": "", "finish_reason": None, "cost_usd": None}
            status, error = exc.kind, str(exc)
            result["http_status"] = exc.http_status
        evidence = reasoning_evidence(summary)
        configuration = "unverified" if args.require_reasoning else "not_required"
        if evidence["observed"]:
            configuration = "observed"
        if status == "completed" and configuration == "unverified":
            error = "추론 사용 근거 없음: 최초 완료 답안을 보존하며 재생성하지 않음"
        result.update(summary, outcome=status, error=error, reasoning_evidence=evidence,
                      configuration_status=configuration, elapsed_s=round(time.monotonic() - start, 3),
                      selected_attempt=number if status == "completed" else None)
        result.update(totals([*history, result]))
        dest = failed if error else out
        common.write_json(dest, result)
        if not error:
            failed.unlink(missing_ok=True)
        record_result(run, task, model, result, dest)
        print(f"[generate] {task} × {model}: {number}/{args.max_attempts} · {status} · {dest}", file=sys.stderr)
        count += 1
        spent = common.add_cost(spent, summary.get("cost_usd"))
        if status not in RETRYABLE:
            break
        history = attempt_history(result)
    return count, spent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-t", "--tasks", nargs="+", required=True)
    parser.add_argument("-m", "--models", nargs="+", required=True)
    parser.add_argument("--results", type=Path, default=common.RESULTS, help="실험 결과 폴더")
    parser.add_argument("--repeat", type=common.positive_int, default=1, help="반복 번호; 반복마다 별도 --results 사용")
    common.add_data_argument(parser)
    llm.add_arguments(parser, timeout=1800)
    parser.set_defaults(token_limit_field=None)
    parser.add_argument("--profile", choices=PROFILES, help="합의한 본실험 생성 설정; 공급사·모델 ID는 별도 지정")
    parser.add_argument("--max-attempts", type=int, choices=(1, 2, 3), default=3,
                        help="과제·모델별 총 시도 상한 (최초 포함, 기본 3)")
    parser.add_argument("--require-reasoning", action="store_true",
                        help="추론 근거 없는 완료 답안을 설정 미확인으로 보관; 재생성하지 않음")
    parser.add_argument("--retry-failed", action="store_true", help="토큰 소진·빈 답안에 남은 시도만 재개")
    parser.add_argument("--dry-run", action="store_true", help="API 없이 입력·예상 비용 확인")
    parser.add_argument("--assume-output", type=common.positive_int, default=15000, help="예상 비용에 사용할 출력 토큰 수")
    args = parser.parse_args()
    try:
        apply_profile(args)
        args.token_limit_field = args.token_limit_field or "max_tokens"
        llm.validate_arguments(args)
        check_profile_body(args)
        common.unique_names(args.tasks, "과제")
        common.unique_names(args.models, "모델")
        for task in args.tasks:
            common.parse_task(task)
    except ValueError as error:
        parser.error(str(error))
    with runs.generation_lock(args.results, enabled=not args.dry_run):
        return run_generation(args)


def run_generation(args):
    jobs = [{"task": task, "model": model} for task in args.tasks for model in args.models]
    count, spent = 0, 0.0
    expected = condition(args)
    check_conditions(args, expected)
    with runs.Run(args, "generate", jobs) as run:
        run.data["generation_condition"] = expected
        run.save()
        prices = load_prices(args.prices, args.base_url)
        messages = {}
        for job in jobs:
            try:
                if job["task"] not in messages:
                    messages[job["task"]] = build_messages(job["task"], args.data_root)
                n, cost = generate_one(args, job["task"], job["model"], run, prices, messages[job["task"]])
                count += n
                spent = common.add_cost(spent, cost)
            except (OSError, ValueError, KeyError, TypeError, LLMError) as error:
                run.record(**job, status="failed", error=str(error))
                print(f"[generate] {job['task']} × {job['model']}: {error}", file=sys.stderr)
    print(f"[generate] 요청 {count}건 · {'예상 ' if args.dry_run else ''}비용 {spent if spent is not None else '알 수 없음'}", file=sys.stderr)
    if not args.dry_run:
        print(f"[generate] 실행 기록: {run.path}", file=sys.stderr)
    return 1 if run.failed else 0


if __name__ == "__main__":
    common.cli(main)
