# 평가자료

제11~15회(2022~2026년) 공법·민사법·형사법 기록형을 대상으로 한다.
기록 15개, 연구용 루브릭 YAML 15개, 읽기용 채점표 15개, 기준답안 15개를 포함한다.

## 파일 안내

| 과제 | 문제·기록 | 루브릭 | 기준답안 |
|---|---|---|---|
| 제11회 민사법 | [기록](records/r11-civil-record.md) | [채점표](rubrics/r11-civil-record.md) | [기준답안](rubrics/r11-civil-record-reference.md) |
| 제11회 형사법 | [기록](records/r11-criminal-record.md) | [채점표](rubrics/r11-criminal-record.md) | [기준답안](rubrics/r11-criminal-record-reference.md) |
| 제11회 공법 | [기록](records/r11-public-record.md) | [채점표](rubrics/r11-public-record.md) | [기준답안](rubrics/r11-public-record-reference.md) |
| 제12회 민사법 | [기록](records/r12-civil-record.md) | [채점표](rubrics/r12-civil-record.md) | [기준답안](rubrics/r12-civil-record-reference.md) |
| 제12회 형사법 | [기록](records/r12-criminal-record.md) | [채점표](rubrics/r12-criminal-record.md) | [기준답안](rubrics/r12-criminal-record-reference.md) |
| 제12회 공법 | [기록](records/r12-public-record.md) | [채점표](rubrics/r12-public-record.md) | [기준답안](rubrics/r12-public-record-reference.md) |
| 제13회 민사법 | [기록](records/r13-civil-record.md) | [채점표](rubrics/r13-civil-record.md) | [기준답안](rubrics/r13-civil-record-reference.md) |
| 제13회 형사법 | [기록](records/r13-criminal-record.md) | [채점표](rubrics/r13-criminal-record.md) | [기준답안](rubrics/r13-criminal-record-reference.md) |
| 제13회 공법 | [기록](records/r13-public-record.md) | [채점표](rubrics/r13-public-record.md) | [기준답안](rubrics/r13-public-record-reference.md) |
| 제14회 민사법 | [기록](records/r14-civil-record.md) | [채점표](rubrics/r14-civil-record.md) | [기준답안](rubrics/r14-civil-record-reference.md) |
| 제14회 형사법 | [기록](records/r14-criminal-record.md) | [채점표](rubrics/r14-criminal-record.md) | [기준답안](rubrics/r14-criminal-record-reference.md) |
| 제14회 공법 | [기록](records/r14-public-record.md) | [채점표](rubrics/r14-public-record.md) | [기준답안](rubrics/r14-public-record-reference.md) |
| 제15회 민사법 | [기록](records/r15-civil-record.md) | [채점표](rubrics/r15-civil-record.md) | [기준답안](rubrics/r15-civil-record-reference.md) |
| 제15회 형사법 | [기록](records/r15-criminal-record.md) | [채점표](rubrics/r15-criminal-record.md) | [기준답안](rubrics/r15-criminal-record-reference.md) |
| 제15회 공법 | [기록](records/r15-public-record.md) | [채점표](rubrics/r15-public-record.md) | [기준답안](rubrics/r15-public-record-reference.md) |

루브릭의 원본은 `rubrics/*-record.yaml`이며, 같은 이름의 Markdown은 읽기용 채점표다.
공통 규칙은 [grading-rules.md](grading-rules.md), 과목별 규칙은 YAML의 `meta.grading_rules`에 있다.
기준답안은 해당 서면의 법리·사실 적용·결론을 설명한다.

## 공개본의 구성

채점에 필요한 기록·규칙·평가항목·점수 조건·기준답안을 포함한다.
교재 대조·출처 줄 번호·설계 메모는 제외했다. 공개본과 내부 기준의 실제 채점 프롬프트가 같은지 모든 과제에서 대조했다.
기준을 바꾸면 버전을 구별하여 새 실험 폴더에서 실행한다.

## 출처와 이용조건

문제·사건기록의 출처는 **법무부 법조인력과**, 저작물은 **2022~2026년도 제11~15회 변호사시험 공법·민사법·형사법 기록형 기출문제**다.
법무부가 공공누리 제1유형으로 공개한 자료를 텍스트로 변환했다. 원문은 [법무부 홈페이지](https://www.moj.go.kr/)의 변호사시험 기출문제 게시판에서 제공한다.
[제15회 기록형 원문 게시물](https://www.moj.go.kr/bbs/moj/150/602399/artclView.do)에서도 출처와 이용조건을 확인할 수 있다.

변환 과정에서 표·글상자의 읽기 순서를 정리하고, 그림·접수 도장·취소선 등의 표기를 텍스트로 옮겼다.
각 기록 파일 첫머리에도 원본 파일명·출처·변환 도구를 표시했다.
문제·사건기록의 재이용에는 [공공누리 제1유형 이용조건](https://www.kogl.or.kr/info/licenseType1.do)을 따른다.
연구진이 직접 작성한 루브릭·기준답안·공통 규칙에는 [MIT 라이선스](../LICENSE)를 적용한다.
