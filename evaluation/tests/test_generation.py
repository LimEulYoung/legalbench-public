"""가상 응답으로 시도 상한·최초 완료 선택·실험 집계를 검증한다."""
from __future__ import annotations

import copy
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common
import generate
import generation_report
import llm
import runs

FIXTURE = Path(__file__).resolve().parents[1] / "examples"
TASK = "r1-public"


def response(content="짧은 답안", finish="stop", cost=0.1):
    return {"model": "served-version", "choices": [{"finish_reason": finish,
            "message": {"content": content, "reasoning_content": "검토"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130, "cost": cost}}


class GenerationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.results = self.root / "experiment"
        self.output = common.answer_file(self.results, TASK, "candidate")
        self.failed = self.output.with_name("candidate.failed.json")

    def invoke(self, *extra, responses=None):
        argv = ["generate.py", "-t", TASK, "-m", "candidate", "--results", str(self.results),
                "--data-root", str(FIXTURE), "--base-url", "https://example.invalid/v1", *extra]
        with patch.object(sys, "argv", argv), \
                patch.object(generate, "load_api_key", return_value="test-only-key") as key, \
                patch.object(generate, "load_prices", return_value={}), \
                patch.object(generate, "chat", side_effect=responses or [response()]) as chat, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = generate.main()
        return code, chat, key

    def test_profiles_send_the_agreed_parameters(self):
        expected = {
            "glm-5.3": ("high", "max_tokens", 131072),
            "deepseek-v4.1-flash": ("high", "max_tokens", 393216),
            "mimo-v2.6-pro": (None, "max_completion_tokens", 131072),
            "qwen3.8-max": ("medium", "max_completion_tokens", 131072),
        }
        for name, (effort, field, limit) in expected.items():
            with self.subTest(profile=name):
                self.results = self.root / name
                code, chat, _ = self.invoke("--profile", name)
                self.assertEqual(code, 0)
                self.assertEqual(chat.call_count, 1)
                result = common.read_json(common.answer_file(self.results, TASK, "candidate"))
                body = result["request"]["body"]
                self.assertEqual(body.get("reasoning_effort"), effort)
                self.assertEqual(body[field], limit)
                self.assertEqual(result["policy"]["max_attempts"], 3)
                if name == "mimo-v2.6-pro":
                    self.assertEqual(body["thinking"], {"type": "enabled"})

    def test_profile_conflicts_are_errors_before_any_request(self):
        for options in (("--reasoning-effort", "low"), ("--max-tokens", "100"),
                        ("--max-attempts", "2"), ("--api-format", "responses"),
                        ("--token-limit-field", "max_completion_tokens")):
            with self.subTest(options=options), self.assertRaises(SystemExit) as caught:
                self.invoke("--profile", "glm-5.3", *options, responses=[AssertionError("API 호출 금지")])
            self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.results.exists())

    def test_dry_run_never_loads_key_or_creates_results(self):
        code, chat, key = self.invoke("--profile", "qwen3.8-max", "--dry-run")
        self.assertEqual(code, 0)
        chat.assert_not_called()
        key.assert_not_called()
        self.assertFalse(self.results.exists())

    def test_short_or_incomplete_legal_content_is_not_regenerated(self):
        code, chat, _ = self.invoke(responses=[response("한 문장")])
        self.assertEqual(code, 0)
        self.assertEqual(chat.call_count, 1)
        result = common.read_json(self.output)
        self.assertEqual(result["selected_attempt"], 1)
        self.assertEqual(result["content"], "한 문장")

    def test_resume_after_saved_failure_uses_only_remaining_attempts(self):
        record = runs.Run.record

        def interrupt_after_save(run, *args, **kwargs):
            record(run, *args, **kwargs)
            raise KeyboardInterrupt

        with patch.object(runs.Run, "record", interrupt_after_save), self.assertRaises(KeyboardInterrupt):
            self.invoke(responses=[response(finish="length")])
        self.assertEqual(common.read_json(self.failed)["attempt"], 1)
        code, chat, _ = self.invoke("--retry-failed", responses=[response(""), response("최종 답안")])
        self.assertEqual(code, 0)
        self.assertEqual(chat.call_count, 2)
        result = common.read_json(self.output)
        self.assertEqual(result["selected_attempt"], 3)
        self.assertEqual([a["outcome"] for a in result["previous_attempts"]], ["length", "empty"])
        self.assertAlmostEqual(result["total_cost_usd"], 0.3)
        report = generation_report.build_report(self.results)["models"]["candidate"]
        self.assertEqual(report["planned"], 1)
        self.assertEqual(report["attempts"], 3)

    def test_interrupted_request_is_counted_and_not_retried(self):
        with self.assertRaises(KeyboardInterrupt):
            self.invoke(responses=[KeyboardInterrupt()])
        code, chat, _ = self.invoke("--retry-failed")
        self.assertEqual(code, 1)
        chat.assert_not_called()
        result = common.read_json(self.failed)
        self.assertEqual((result["attempt"], result["outcome"]), (1, "interrupted"))
        self.assertIsNone(result["total_cost_usd"])

    def test_concurrent_generation_cannot_overwrite_the_same_experiment(self):
        with runs.generation_lock(self.results), self.assertRaisesRegex(ValueError, "이미 실행 중"):
            self.invoke(responses=[AssertionError("API 호출 금지")])
        code, chat, _ = self.invoke()
        self.assertEqual((code, chat.call_count), (0, 1))

    def test_unknown_failed_cost_stays_unknown_after_success(self):
        code, _, _ = self.invoke(responses=[response(finish="length", cost=None), response()])
        self.assertEqual(code, 0)
        result = common.read_json(self.output)
        self.assertIsNone(result["total_cost_usd"])
        report = generation_report.build_report(self.results)["models"]["candidate"]
        self.assertIsNone(report["total_cost_usd"])
        self.assertEqual(report["unknown_cost_attempts"], 1)
        self.assertEqual(report["known_cost_usd"], 0.1)

    def test_refusal_and_http_errors_do_not_trigger_generation_retries(self):
        refusal = response("")
        refusal["choices"][0]["message"]["refusal"] = "거절"
        for name, answer in (("refusal", refusal),
                             ("http_error", llm.LLMError("HTTP 429", kind="http_error", http_status=429))):
            with self.subTest(outcome=name):
                self.results = self.root / name
                code, chat, _ = self.invoke(responses=[answer])
                self.assertEqual((code, chat.call_count), (1, 1))
                report = generation_report.build_report(self.results)["models"]["candidate"]
                self.assertEqual(report["attempt_outcomes"], {name: 1})

    def test_settings_are_fixed_across_tasks_and_resumed_runs(self):
        self.invoke("--reasoning-effort", "high")
        for options in (("--reasoning-effort", "low"), ("--max-attempts", "2")):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "생성 설정"):
                self.invoke("-t", "r2-public", *options, responses=[AssertionError("API 호출 금지")])

    def test_report_keeps_missing_tasks_and_does_not_count_resumes_twice(self):
        self.invoke()
        self.invoke()
        report = generation_report.build_report(self.results, [TASK, "r2-public"], ["candidate"])
        model = report["models"]["candidate"]
        self.assertEqual(model["planned"], 2)
        self.assertEqual(model["attempts"], 1)
        self.assertEqual(model["first_attempt_completion_rate"], 0.5)
        self.assertEqual(model["completion_rate"], 0.5)
        self.assertEqual(model["incomplete_tasks"], ["r2-public"])
        self.assertIsNone(model["total_cost_usd"])
        self.assertEqual(model["known_cost_usd"], 0.1)

    def test_report_rejects_changed_retry_parameters_and_completed_retries(self):
        self.invoke(responses=[response(finish="length"), response()])
        original = common.read_json(self.output)
        for field, value in (("outcome", "completed"), ("params", {})):
            data = copy.deepcopy(original)
            data["previous_attempts"][0][field] = value
            common.write_json(self.output, data)
            with self.subTest(field=field), self.assertRaises(ValueError):
                generation_report.build_report(self.results)

    def test_unknown_responses_incomplete_reason_is_not_a_token_limit_retry(self):
        data = llm._normalize_response({"status": "incomplete", "output": []})
        self.assertEqual(generate.outcome(llm.summarize(data, {}, "m")), "unexpected_finish")


if __name__ == "__main__":
    unittest.main()
