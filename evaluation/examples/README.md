# 외부 자료 없이 실행하기

이 폴더는 프로그램 검증을 위한 가상 안내문·기준답안·채점표다. 실제 시험 문제나 법률 평가 자료가 아니다.
아래 명령은 저장소 루트에서 실행한다. 공개 코드 묶음도 같은 `evaluation/` 구조를 사용한다.

```bash
python3 -m pip install -r evaluation/requirements.txt
python3 -B evaluation/judge.py -t r1-public \
  --data-root evaluation/examples --answer-file evaluation/examples/answer.md \
  --name sample --judge preview --max-tokens 4096 --dry-run --dump /tmp/legalbench-demo
python3 -B -m unittest discover -s evaluation/tests -p test_runtime.py -v
```

프롬프트 미리보기와 테스트는 외부 API를 호출하지 않는다. 테스트는 로컬 HTTP 서버로
생성·채점·합산·실패 복구를 검증하고 임시 파일을 정리한다. 내부 시험 자료나 API 키가 필요하지 않다.

실제 모델로 이 가상 예제를 채점하려면 모델 ID와 API 설정을 지정하고 `--dry-run`을 뺀다.
이 경우 해당 제공자의 API 비용이 발생한다. 실행 인자는 `python3 evaluation/judge.py --help`로 확인한다.
