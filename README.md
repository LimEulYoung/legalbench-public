# 변호사시험 기록형 LLM 평가

한국 변호사시험 기록형 문제로 **LLM의 법률문서 작성 능력**을 평가하는 연구다.
사건기록에 근거한 소장·검토의견서·변론요지서를 루브릭과 기준답안으로 채점한다.

## 공개 내용

| 자료 | 위치 |
|---|---|
| 시험 기록·루브릭·기준답안 | [data/](data/README.md) |
| 답안 생성·자동 채점·점수 집계·사람 채점 비교 | [evaluation/](evaluation/README.md) |
| API 없이 실행할 수 있는 가상 예제 | [evaluation/examples/](evaluation/examples/README.md) |

현재 평가자료는 연구용 루브릭 **v2.0**이다. 본실험과 외부 전문가의 독립 채점 검증은 아직 수행하지 않았다.
공식 시험 채점표가 아니며, 평가항목의 배점과 부분점수는 연구 목적으로 구성했다.

## 설치와 확인

Python 3.10 이상, Linux 또는 macOS를 사용한다. 저장소 루트에서 실행한다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
make check
```

검사는 외부 API를 호출하지 않는다. 실제 모델 실행 방법은 [평가 코드 안내](evaluation/README.md)에 있다.
API 키는 환경변수로 전달하고, `.env`·실험 결과·사람 채점표는 Git에 넣지 않는다.

## 라이선스

직접 작성한 평가 코드·루브릭·기준답안은 [MIT 라이선스](LICENSE)로 제공한다.
법무부의 시험 문제·사건기록에는 **공공누리 제1유형(출처표시)**을 적용한다. 자세한 출처와 변환 사항은 [자료 안내](data/README.md#출처와-이용조건)를 따른다.
시판 교재 원문, 모의시험 채점기준표, 내부 검토 자료와 실험 원시 응답은 공개본에 포함하지 않는다.
