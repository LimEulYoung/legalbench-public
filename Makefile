PYTHON ?= python3
.DEFAULT_GOAL := help
.PHONY: help check test-public check-rubrics

help:
	@printf '%s\n' 'make check          공개 코드 테스트와 루브릭 검사' 'make test-public    가상 자료만 사용한 독립 실행 검사'

check: test-public check-rubrics

test-public:
	$(PYTHON) -B evaluation/tests/run_public.py

check-rubrics:
	$(PYTHON) -B evaluation/rubric.py data/rubrics/*.yaml --check
