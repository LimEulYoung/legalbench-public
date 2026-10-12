# 답안 생성과 평가

한 과목의 기록·답안·채점 규칙·루브릭·기준답안을 함께 제공해 채점한다.
LLM은 항목별 점수와 근거를 반환하고, 코드는 응답을 검증하고 총점을 계산한다.

## 구성

| 파일 | 역할 |
|---|---|
| `generate.py` | 답안 생성, 제한 재시도, 최초 완료 답안 선택 |
| `judge.py`·`grading.py`·`prompts.py` | 채점 입력 구성, 요청, 응답 검증 |
| `score.py` | 저장된 점수 검증·합산과 CSV 출력 |
| `generation_report.py` | 생성 완료 여부·시도·사용량 집계 |
| `human_sheet.py`·`agreement.py` | 익명 사람 채점표와 일치도 비교 |
| `llm.py`·`common.py`·`runs.py` | API 통신, 파일·인자 처리, 실행 이력 |
| `rubric.py` | 루브릭 검증과 읽기용 채점표 생성 |

## API 없이 확인하기

```bash
make check
python3 -B evaluation/judge.py -t r1-public \
  --data-root evaluation/examples --answer-file evaluation/examples/answer.md \
  --name sample --judge preview --max-tokens 4096 --dry-run
```

## 생성·채점·집계

각 스크립트의 `--help`에서 전체 인자를 확인한다. `--dry-run`은 API를 호출하지 않는다.
아래의 모델 ID와 API 주소는 사용할 공급사에 맞게 지정한다. 키는 `LLM_API_KEY` 환경변수로 전달한다.

```bash
python3 -B evaluation/generate.py -t r15-public -m GENERATION_MODEL \
  --base-url https://PROVIDER_BASE_URL/v1 --api-key-env LLM_API_KEY \
  --results results/experiment --max-tokens 131072 --dry-run

python3 -B evaluation/judge.py -t r15-public -m GENERATION_MODEL \
  --judge gpt-6-astra --profile gpt-6-astra \
  --base-url https://PROVIDER_BASE_URL/v1 --api-key-env LLM_API_KEY \
  --results results/experiment --dry-run

python3 -B evaluation/score.py --judge gpt-6-astra \
  --results results/experiment --detail --csv results/experiment/scores.csv
```

생성·채점 명령의 `--dry-run`을 제거하면 실제 요청을 전송한다. 합산은 완료된 채점 결과가 있을 때 실행한다.
`--data-root`로 다른 자료 묶음을 지정할 수 있다. 기존 텍스트 답안은 채점 명령의 `--answer-file`·`--name`으로 받는다.

## 모델 프로필

| 생성 `--profile` | 추론 설정 | 출력 상한 |
|---|---|---:|
| `glm-5.3` | high | 131,072 |
| `deepseek-v4.1-flash` | high | 393,216 |
| `mimo-v2.6-pro` | thinking enabled | 131,072 |
| `qwen3.8-max` | medium | 131,072 |

생성 프로필은 최초 요청을 포함한 최대 3회와 추론 사용 확인을 적용한다.
토큰 소진·빈 최종 답안만 동일한 조건으로 재시도하며 최초 정상 완료 답안을 사용한다.
API·통신 오류와 거절 등에는 자동 생성 재시도를 하지 않는다. 실제 공급사의 모델 ID·지원 필드·출력 상한은 실행 전에 확인한다.

채점 `--profile gpt-6-astra`는 Responses API, 추론 강도 `high`, 출력 상한 128,000토큰을 적용한다.
프로필과 충돌하는 개별 인자는 거부한다.

## 저장과 검증

`answers/`에 답안과 시도 이력, `judgments/`에 항목별 판정, `runs/`에 실행 설정을 저장한다.
응답 항목의 누락·중복, 허용되지 않은 점수, 답안에 없는 인용, 부적절한 기록 위치를 검사한다.
실패나 검토 보류를 0점으로 바꾸지 않으며, 사용량·비용을 알 수 없으면 `null`로 남긴다.
기록·루브릭·프롬프트·설정이 바뀌면 새 결과 폴더를 사용한다.

`score.py --source saved`는 결과에 저장된 당시 자료로 합산한다.
`--source current`는 현재 자료와의 일치 여부도 확인한다.

## 사람 채점과 일치도

`human_sheet.py`는 익명 답안과 Excel 채점표를 생성한다.
채점자는 `채점` 시트의 I열에 점수, J열에 메모를 입력한다. 대응표 `key.json`은 채점 전에 열지 않는다.

```bash
python3 -B evaluation/human_sheet.py -t r15-public -m MODEL_A MODEL_B \
  --results results/experiment --rater reviewer_a
python3 -B evaluation/human_sheet.py -t r15-public -m MODEL_A MODEL_B \
  --results results/experiment --rater reviewer_b
python3 -B evaluation/agreement.py -t r15-public -m MODEL_A MODEL_B \
  --results results/experiment --human-raters reviewer_a reviewer_b
```

이견 협의 시 독립 점수는 보존하고 `--rater consensus`로 별도 합의표를 만든다.
합의 점수 입력 후 `agreement.py --judges gpt-6-astra --human-raters consensus`로 AI 점수와 비교한다.
항목별 정확 일치율, 배점으로 정규화한 평균 절대 차이, 구간 척도 Krippendorff α를 계산한다.
