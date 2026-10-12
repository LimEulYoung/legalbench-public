"""Chat Completions·Responses 호환 HTTP/SSE 요청과 사용량·비용 정규화."""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from contextlib import nullcontext
from pathlib import Path

import common

BASE_URL = "https://gw.letsur.ai/v1"
KEY_FILE = common.ROOT / "scripts/.env"
PRICE_CACHE = common.ROOT / "scripts/models.json"


class LLMError(RuntimeError):
    """실패 이유와 실패 전 수신한 응답."""

    def __init__(self, message, response=None, *, kind="transport_error", http_status=None):
        super().__init__(message)
        self.response = response or {}
        self.kind = kind
        self.http_status = http_status


def add_arguments(parser, timeout=900):
    parser.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", BASE_URL),
                        help="API 주소 (/v1까지, 환경변수 LLM_BASE_URL)")
    parser.add_argument("--api-key-env", help="키 환경변수 이름 (LetSur: LETSUR_API_KEY, 그 밖의 주소: LLM_API_KEY)")
    parser.add_argument("--api-key-file", type=Path, help="키 환경변수의 NAME=VALUE를 담은 파일")
    parser.add_argument("--prices", type=Path, help="가격 JSON (기본 LetSur 주소에서만 저장소 캐시 사용)")
    parser.add_argument("--max-tokens", type=common.positive_int,
                        help="출력 토큰 상한 (프로필 없이 생략하면 API 기본값)")
    parser.add_argument("--token-limit-field", choices=("max_tokens", "max_completion_tokens"),
                        default="max_tokens", help="Chat API에 출력 상한을 전달할 필드")
    parser.add_argument("--temperature", type=common.nonnegative_float, help="생략 시 API 기본값")
    parser.add_argument("--reasoning-effort", help="API가 지원하는 추론 강도")
    parser.add_argument("--extra-body", type=Path,
                        help="공급사별 추가 요청 필드를 담은 JSON 파일 (기본 요청 필드 덮어쓰기 불가)")
    parser.add_argument("--api-format", choices=("chat", "responses"), default="chat",
                        help="API 형식: Chat Completions 또는 Responses")
    parser.add_argument("--seed", type=int, help="API가 지원하는 시드 (재현성 보장은 제공자에 따름)")
    parser.add_argument("--timeout", type=common.positive_int, default=timeout,
                        help="연결·응답 조각 간 대기 제한(초)")
    parser.add_argument("--context-window", type=common.positive_int,
                        help="전체 토큰 한도 (--max-tokens와 함께 사용)")


def validate_arguments(args):
    url = urllib.parse.urlsplit(args.base_url)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("--base-url은 자격 증명·쿼리가 없는 HTTP(S) 주소여야 함")
    if args.context_window and args.max_tokens is None:
        raise ValueError("--context-window에는 --max-tokens도 필요함")
    if args.api_format == "responses" and args.token_limit_field != "max_tokens":
        raise ValueError("Responses의 토큰 상한은 max_output_tokens로 전송함; --token-limit-field는 Chat 전용")
    args.api_key_env = args.api_key_env or ("LETSUR_API_KEY" if args.base_url.rstrip("/") == BASE_URL else "LLM_API_KEY")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env):
        raise ValueError("--api-key-env에는 환경변수 이름을 지정해야 함")
    if args.extra_body is not None:
        try:
            args.extra_body = common.read_json(args.extra_body)
        except OSError as error:
            raise ValueError(f"--extra-body 파일을 읽을 수 없음: {error}") from error
        validate_extra_body(args.extra_body)
        reasoning = args.extra_body.get("reasoning")
        if (isinstance(reasoning, dict) and "effort" in reasoning
                and args.reasoning_effort is not None and reasoning["effort"] != args.reasoning_effort):
            raise ValueError("--reasoning-effort와 --extra-body의 reasoning.effort가 다름")


def validate_extra_body(body):
    if not isinstance(body, dict):
        raise ValueError("--extra-body는 JSON 객체여야 함")
    reserved = {"model", "messages", "max_tokens", "temperature", "reasoning_effort", "seed",
                "input", "instructions", "max_output_tokens", "max_completion_tokens",
                "stream", "stream_options", "api_key", "authorization", "headers"}
    if reserved.intersection(key.lower() for key in body):
        raise ValueError("--extra-body에 기본 요청 필드나 인증 정보를 지정할 수 없음")
    if "reasoning" in body and not isinstance(body["reasoning"], dict):
        raise ValueError("--extra-body의 reasoning은 JSON 객체여야 함")
    if "n" in body and (type(body["n"]) is not int or body["n"] != 1):
        raise ValueError("한 요청에는 답안 하나만 생성할 수 있음 (n=1)")


def parameters(args):
    result = {"base_url": args.base_url.rstrip("/"), "temperature": args.temperature,
              "reasoning_effort": args.reasoning_effort, "max_tokens": args.max_tokens,
              "seed": args.seed, "timeout": args.timeout, "stream": True}
    if args.extra_body is not None:
        result["extra_body"] = args.extra_body
    if args.api_format != "chat":
        result["api_format"] = args.api_format
    if args.token_limit_field != "max_tokens":
        result["token_limit_field"] = args.token_limit_field
    return result


def check_budget(messages, args):
    tokens = sum(common.est_tokens(m["content"]) for m in messages)
    if args.context_window and tokens + args.max_tokens > args.context_window:
        raise ValueError(f"추정 입력 {tokens:,} + 출력 상한 {args.max_tokens:,}이 "
                         f"전체 한도 {args.context_window:,}를 넘음. 입력은 자르지 않음")
    return tokens


def load_api_key(env_name="LETSUR_API_KEY", key_file=None):
    key = os.environ.get(env_name, "").strip()
    if key:
        return key
    path = key_file or (KEY_FILE if env_name == "LETSUR_API_KEY" else None)
    if path and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == env_name and value.strip():
                return value.strip()
    raise LLMError(f"API 키 없음: 환경변수 {env_name} 또는 --api-key-file 확인")


def load_prices(path=None, base_url=BASE_URL):
    if path is not None and not path.is_file():
        raise ValueError(f"가격 파일 없음: {path}")
    path = path or (PRICE_CACHE if base_url.rstrip("/") == BASE_URL else None)
    if not path or not path.exists():
        return {}
    prices = {}
    for model in common.read_json(path):
        currency = (model.get("pricing") or {}).get("currency")
        if currency is not None and currency.lower() != "usd":
            continue
        usage = (model.get("pricing") or {}).get("usage") or []
        pin = next((u["amount"] for u in usage if u.get("unit") == "token_input"), None)
        pout = next((u["amount"] for u in usage if u.get("unit") == "token_output"), None)
        if _number(pin) and _number(pout):
            prices[model["id"]] = (pin, pout)
    return prices


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _read_stream(response):
    """SSE 이벤트를 합친다. 손상·중단은 부분 응답을 첨부한 오류로 반환한다."""
    content, reasoning, refusal, event = [], [], [], []
    result = {"usage": {}, "choices": [{"message": {}, "finish_reason": None}]}

    def snapshot():
        result["choices"][0]["message"] = {"content": "".join(content), "reasoning_content": "".join(reasoning)}
        if refusal:
            result["choices"][0]["message"]["refusal"] = "".join(refusal)
        return result

    def consume(data):
        if data == "[DONE]":
            if result["choices"][0]["finish_reason"] is None:
                raise ValueError("종료 사유 없는 스트림")
            return True
        chunk = json.loads(data, parse_constant=common.reject_constant)
        if not isinstance(chunk, dict):
            raise ValueError("API 스트림 응답이 JSON 객체가 아님")
        if chunk.get("error"):
            detail = json.dumps(chunk["error"], ensure_ascii=False)
            raise ValueError(f"API 스트림 오류: {detail}")
        for key in ("id", "model", "system_fingerprint", "usage", "estimated_cost"):
            if chunk.get(key) is not None:
                result[key] = chunk[key]
        choices = chunk.get("choices") or []
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError("단일 응답 스트림이 필요함")
        for choice in choices:
            if choice.get("index", 0) != 0:
                raise ValueError("알 수 없는 응답 인덱스")
            delta = choice.get("delta") or {}
            for value, target in ((delta.get("content"), content),
                                  (delta.get("reasoning_content") or delta.get("reasoning"), reasoning),
                                  (delta.get("refusal"), refusal)):
                if value is not None:
                    if not isinstance(value, str):
                        raise ValueError("문자열이 아닌 응답 조각")
                    target.append(value)
            if choice.get("finish_reason"):
                result["choices"][0]["finish_reason"] = choice["finish_reason"]
        return False

    try:
        for raw in response:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                event.append(line[5:].lstrip(" "))
            elif not line and event:
                data, event = "\n".join(event), []
                if consume(data):
                    return snapshot()
            elif line and not line.startswith((":", "event:", "id:", "retry:")):
                raise ValueError("잘못된 SSE 행")
        if event and consume("\n".join(event)):
            return snapshot()
        raise ValueError("[DONE] 전에 스트림이 끝남")
    except (ValueError, TypeError, AttributeError, OSError, http.client.HTTPException) as error:
        raise LLMError(f"스트림 수신 실패: {error}", snapshot(), kind="stream_error") from error


def _normalize_response(data):
    """Responses의 상태·본문·추론·사용량을 공통 응답 형식으로 변환한다."""
    if not isinstance(data, dict):
        raise ValueError("Responses 응답이 JSON 객체가 아님")
    if not isinstance(data.get("output") or [], list) or not isinstance(data.get("usage") or {}, dict):
        raise ValueError("Responses 본문·사용량 형식 오류")
    content, reasoning, refusal = [], [], []
    for item in data.get("output") or []:
        target = reasoning if item.get("type") == "reasoning" else content
        if item.get("type") not in {"reasoning", "message"}:
            continue
        for part in item.get("summary") or item.get("content") or []:
            if isinstance(part.get("text"), str):
                target.append(part["text"])
            if part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                refusal.append(part["refusal"])
    raw_usage = data.get("usage") or {}
    usage = {dest: raw_usage[src] for src, dest in (
        ("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"), ("input_tokens_details", "prompt_tokens_details"),
        ("output_tokens_details", "completion_tokens_details"), ("cost", "cost")) if src in raw_usage}
    status = data.get("status")
    reason = (data.get("incomplete_details") or {}).get("reason")
    finish = {"completed": "stop", "incomplete": reason or "incomplete"}.get(status)
    if finish == "max_output_tokens":
        finish = "length"
    result = {"id": data.get("id"), "model": data.get("model"), "usage": usage,
              "response_status": status, "estimated_cost": data.get("estimated_cost"),
              "choices": [{"message": {"content": "".join(content), "reasoning_content": "".join(reasoning)},
                           "finish_reason": finish}]}
    if refusal:
        result["choices"][0]["message"]["refusal"] = "".join(refusal)
    if status == "failed" or data.get("error"):
        result["error"] = data.get("error") or {"message": "Responses 상태가 failed임"}
    return result


def _read_responses_stream(response):
    """최종 response.status를 확인하며, 중단 시 수신한 내용을 보존한다."""
    state, content, reasoning, refusal, event = {}, [], [], [], []

    def snapshot():
        try:
            result = _normalize_response(state)
        except (ValueError, TypeError, AttributeError):
            result = _normalize_response({k: state.get(k) for k in ("id", "model", "status")})
        message = result["choices"][0]["message"]
        if content:
            message["content"] = "".join(content)
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if refusal:
            message["refusal"] = "".join(refusal)
        return result

    def finish():
        _normalize_response(state)
        result = snapshot()
        if result.get("error"):
            raise LLMError("Responses 실패: " + json.dumps(result["error"], ensure_ascii=False), result,
                           kind="api_error")
        return result

    def consume(text):
        chunk = json.loads(text, parse_constant=common.reject_constant)
        kind = chunk.get("type", "")
        if kind == "error" or chunk.get("error"):
            raise ValueError("Responses 오류: " + json.dumps(chunk, ensure_ascii=False))
        if isinstance(chunk.get("response"), dict):
            state.update(chunk["response"])
        if kind in {"response.output_text.delta", "response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
            delta = chunk.get("delta")
            if not isinstance(delta, str):
                raise ValueError("문자열이 아닌 Responses 출력 조각")
            (content if kind == "response.output_text.delta" else reasoning).append(delta)
        if kind == "response.refusal.delta":
            if not isinstance(chunk.get("delta"), str):
                raise ValueError("문자열이 아닌 Responses 거절 조각")
            refusal.append(chunk["delta"])
        if kind in {"response.completed", "response.incomplete", "response.failed"}:
            if state.get("status") not in {"completed", "incomplete", "failed"}:
                raise ValueError("최종 상태가 없는 Responses 종료 이벤트")
            return True
        return False

    try:
        for raw in response:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                event.append(line[5:].lstrip(" "))
            elif not line and event:
                data, event = "\n".join(event), []
                if consume(data):
                    return finish()
            elif line and not line.startswith((":", "event:", "id:", "retry:")):
                raise ValueError("잘못된 Responses SSE 행")
        if event and consume("\n".join(event)):
            return finish()
        raise ValueError("최종 상태 전에 Responses 스트림이 끝남")
    except (ValueError, TypeError, AttributeError, OSError, http.client.HTTPException) as error:
        raise LLMError(f"Responses 스트림 수신 실패: {error}", snapshot(), kind="stream_error") from error


def build_request(messages, model, max_tokens=None, temperature=None, reasoning_effort=None,
                  timeout=900, stream=False, base_url=BASE_URL, seed=None, extra_body=None,
                  api_format="chat", token_limit_field="max_tokens"):
    """인증 정보를 제외한 실제 요청 주소와 본문을 만든다."""
    if api_format not in {"chat", "responses"}:
        raise ValueError("알 수 없는 API 형식")
    if token_limit_field not in {"max_tokens", "max_completion_tokens"}:
        raise ValueError("알 수 없는 토큰 상한 필드")
    if api_format == "responses" and token_limit_field != "max_tokens":
        raise ValueError("token_limit_field는 Chat 전용")
    payload = {"model": model, "messages" if api_format == "chat" else "input": messages}
    payload.update({k: v for k, v in {"temperature": temperature, "seed": seed}.items() if v is not None})
    if max_tokens is not None:
        payload[token_limit_field if api_format == "chat" else "max_output_tokens"] = max_tokens
    if reasoning_effort is not None:
        payload.update({"reasoning_effort": reasoning_effort} if api_format == "chat"
                       else {"reasoning": {"effort": reasoning_effort}})
    if stream:
        payload["stream"] = True
        if api_format == "chat":
            payload["stream_options"] = {"include_usage": True}
    if extra_body is not None:
        validate_extra_body(extra_body)
        extra_body = dict(extra_body)
        effort = extra_body.get("reasoning", {}).get("effort", reasoning_effort)
        if reasoning_effort is not None and effort != reasoning_effort:
            raise ValueError("reasoning.effort와 reasoning_effort가 다름")
        if api_format == "responses" and "reasoning" in extra_body:
            payload["reasoning"] = {**payload.get("reasoning", {}), **extra_body.pop("reasoning")}
        payload.update(extra_body)
    endpoint = "/chat/completions" if api_format == "chat" else "/responses"
    return {"url": base_url.rstrip("/") + endpoint, "body": payload}


def _capture_lines(response, capture, key):
    for line in response:
        if capture:
            capture.write(line.replace(key.encode(), b"[redacted]"))
            capture.flush()
        yield line


def _gateway_id(headers):
    return headers.get("x-letsur-request-id") or headers.get("x-request-id")


def chat(key, messages, model, *, retries=0, raw_response_path=None, **params):
    """요청 한 번을 실행한다. 원시 응답을 보존하며 자동 재호출하지 않는다."""
    if retries != 0:
        raise ValueError("전송 계층은 자동 재시도를 지원하지 않음")
    prepared = build_request(messages, model, **params)
    request = urllib.request.Request(prepared["url"],
        data=json.dumps(prepared["body"], allow_nan=False).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    api_format = params.get("api_format", "chat")
    gateway_id = None
    if raw_response_path:
        raw_response_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with (raw_response_path.open("xb") if raw_response_path else nullcontext()) as capture, \
                urllib.request.urlopen(request, timeout=params.get("timeout", 900)) as response:
            gateway_id = _gateway_id(getattr(response, "headers", {}))
            if params.get("stream"):
                lines = _capture_lines(response, capture, key)
                data = (_read_stream if api_format == "chat" else _read_responses_stream)(lines)
            else:
                raw = response.read()
                if capture:
                    capture.write(raw.replace(key.encode(), b"[redacted]"))
                data = json.loads(raw, parse_constant=common.reject_constant)
                if api_format == "responses":
                    data = _normalize_response(data)
            if not isinstance(data, dict):
                raise ValueError("정상 응답 JSON 객체가 아님")
            if data.get("error"):
                raise LLMError("API 오류: " + json.dumps(data["error"], ensure_ascii=False), data, kind="api_error")
            data["gateway_request_id"] = gateway_id
            return data
    except LLMError as error:
        detail = str(error).replace(key, "[redacted]")[:1000]
        safe_response = json.loads(json.dumps(error.response).replace(key, "[redacted]"))
        safe_response["gateway_request_id"] = gateway_id
        raise LLMError(detail, safe_response, kind=error.kind, http_status=error.http_status) from None
    except urllib.error.HTTPError as error:
        raw = error.read().replace(key.encode(), b"[redacted]")
        if raw_response_path:
            raw_response_path.write_bytes(raw)
        detail = raw.decode("utf-8", "replace")[:500]
        response = {"gateway_request_id": _gateway_id(error.headers or {})}
        raise LLMError(f"HTTP {error.code}: {detail}", response,
                       kind="http_error", http_status=error.code) from None
    except (urllib.error.URLError, socket.timeout, OSError, http.client.HTTPException, ValueError) as error:
        detail = str(error).replace(key, "[redacted]")[:1000]
        raise LLMError(f"API 요청 실패: {detail}") from error


def summarize(response, prices, requested):
    """사용량이 완전할 때만 비용을 추정한다. 누락된 사용량·비용은 null이다."""
    try:
        choices = response.get("choices") or [{}]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("단일 응답이 필요함")
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        usage = response.get("usage") or {}
        if not isinstance(content, str) or not isinstance(usage, dict):
            raise ValueError("본문·사용량 형식 오류")
    except (AttributeError, TypeError, IndexError, ValueError) as error:
        raise LLMError(f"응답 형식 오류: {error}", response, kind="response_error") from error
    model = response.get("model")
    price = prices.get(model) or prices.get(requested)
    cost = usage.get("cost")
    source = "reported" if _number(cost) else None
    if source is None:
        cost = None
        if price and all(_number(usage.get(k)) for k in ("prompt_tokens", "completion_tokens")):
            cost = usage["prompt_tokens"] * price[0] + usage["completion_tokens"] * price[1]
            source = "estimated"
    return {"served_model": model, "response_id": response.get("id"),
            "gateway_request_id": response.get("gateway_request_id"),
            "response_status": response.get("response_status"), "gateway_cost": response.get("estimated_cost"),
            "system_fingerprint": response.get("system_fingerprint"), "content": content,
            "reasoning": message.get("reasoning_content") or message.get("reasoning") or "",
            "refusal": message.get("refusal"),
            "finish_reason": choice.get("finish_reason"), "usage": usage,
            "cost_usd": cost, "cost_source": source}
