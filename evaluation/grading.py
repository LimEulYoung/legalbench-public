"""과목별 채점의 입력 자료·프롬프트 구성과 형식 검증. 법률적 점수는 판단하지 않는다."""
from __future__ import annotations

import re
from collections import Counter

import common
import prompts
import rubric


def entries(rb):
    """(문서 ID, 쟁점 ID 또는 None, 평가항목)."""
    for doc in rb["documents"]:
        for issue in doc["issues"]:
            for item in rubric.items(issue):
                yield doc["id"], issue["id"], item
        if doc.get("discretion"):
            yield doc["id"], None, doc["discretion"]


def load_context(rb, root=None):
    """각 파일을 한 번 읽어 프롬프트와 해시에 같은 내용을 사용한다."""
    meta = rb["meta"]
    sources = {}
    texts = {}
    for field in ("guidelines", "reference_guide"):
        if not meta.get(field):
            raise ValueError(f"일괄 채점에 meta.{field}가 필요함")
        path = (root or common.ROOT) / meta[field]
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError(f"채점 자료가 비어 있음: {path}")
        texts[field] = text
        sources[field] = {"path": meta[field], "sha": common.sha(text)}
    # 연구용 설명·검토 이력은 보내지 않고, 편집 가능한 판정 규칙 절 전체를 읽는다.
    match = re.search(r"(?ms)^## 판정 규칙\s*\n(.*?)(?=^## |\Z)", texts["guidelines"])
    if not match or not match[1].strip():
        raise ValueError("공통 가이드라인에 비어 있지 않은 '## 판정 규칙' 절이 필요함")
    rules = [match[1].strip()]
    rules.extend(f"- {rule}" for rule in meta["grading_rules"])
    rules.extend(f"- {scope}" for scope in meta.get("scope", []))
    excerpts = rubric.reference_excerpts(rb, texts["reference_guide"])
    return {"rules": "\n\n".join(rules), "reference": texts["reference_guide"], "reference_excerpts": excerpts, "sources": sources,
            "source_texts": texts}


def source_hashes(sources):
    return {key: value["sha"] for key, value in sources.items()}


def messages(rb, answer, record, context, format_item):
    lines = []
    for doc in rb["documents"]:
        lines.append(f"### {doc['id']} {doc['title']} ({doc['points']:g}점)")
        for issue in doc["issues"]:
            lines.append(f"#### {issue['id']} {issue['title']} ({issue['points']:g}점)")
            if issue.get("reference_sections"):
                lines.append("기준답안 위치: " + "; ".join(issue["reference_sections"]))
            for section in issue["sections"]:
                lines.append(f"##### {section['title']} ({section['points']:g}점)")
                lines.extend(format_item(item) for item in section["items"])
        if doc.get("discretion"):
            lines.append(format_item(doc["discretion"]))
    return [
        {"role": "system", "content": prompts.TASK_JUDGE_SYSTEM.format(
            subject=rb["meta"]["subject"],
            legal_cutoff=rb["meta"].get("legal_cutoff") or common.exam_month(rb["meta"]["id"]),
            grading_rules=context["rules"])},
        {"role": "user", "content": prompts.TASK_JUDGE_USER.format(
            answer=answer, record=record, reference_answer=context["reference"],
            rubric_text="\n\n".join(lines))},
    ]


def template_sha():
    return common.sha(prompts.TASK_JUDGE_SYSTEM + prompts.TASK_JUDGE_USER)


def is_provisional(rb):
    """채점 규칙의 확정 여부를 표시한다. 채점 신뢰성의 실험 검증과는 별개다."""
    status = rb["meta"].get("scoring_status")
    finalized = status == "finalized" if status is not None else bool(rb["meta"].get("reviewed_by"))
    return (not finalized
            or any(issue.get("flag") == "review" for _, issue in rubric.issues(rb))
            or any(item.get("flag") == "review" for _, _, item in entries(rb)))


def quote_locations(quotes, answer):
    """공백 차이를 허용하는 직접 인용을 원문 줄 범위로 연결한다. 첫 출현 위치를 기록한다."""
    offsets = [n for n, char in enumerate(answer) if not char.isspace()]
    compact = "".join(answer[n] for n in offsets)
    locations = []
    for quote in quotes:
        needle = "".join(quote.split())
        start = compact.find(needle)
        if start >= 0 and needle:
            locations.append({"start_line": answer.count("\n", 0, offsets[start]) + 1,
                              "end_line": answer.count("\n", 0, offsets[start + len(needle) - 1]) + 1})
    return locations


def check_response(data, rb, answer, record, check_item):
    """일괄 응답을 항목별 합산 구조로 옮긴다. 누락·중복·미결은 0점으로 만들지 않는다."""
    expected = {item["id"] for _, _, item in entries(rb)}
    errors, by_id = [], {}
    if not isinstance(data, dict) or set(data) != {"items"} or not isinstance(data["items"], list):
        errors.append("items 배열 하나를 가진 JSON 객체가 필요함")
        raw_items = []
    else:
        raw_items = data["items"]
    ids = [row["id"] for row in raw_items if isinstance(row, dict) and isinstance(row.get("id"), str)]
    counts = Counter(ids)
    for iid in sorted(set(ids) - expected):
        errors.append(f"알 수 없는 항목: {iid}")
    if len(ids) != len(raw_items):
        errors.append("항목 객체 또는 문자열 ID가 아닌 응답이 있음")
    for row in raw_items:
        if isinstance(row, dict) and isinstance(row.get("id"), str) and counts[row["id"]] == 1:
            by_id[row["id"]] = row
    issues, discretion = {}, {}
    core = {"id", "awarded", "quotes", "record_lines", "reason"}
    extra = {"alternative", "review_required", "review_reason"}
    for doc_id, issue_id, item in entries(rb):
        iid = item["id"]
        raw = by_id.get(iid)
        problems = []
        pending = False
        if counts[iid] > 1:
            problems.append("항목 ID 중복")
        if not isinstance(raw, dict) or set(raw) != core | extra:
            row, _ = check_item(None, item, answer, record)
            problems.append("항목 누락 또는 응답 필드 불일치")
        else:
            pending = raw["review_required"] is True
            core_data = {key: raw[key] for key in core}
            # 보류 항목도 인용·기록 위치·이유는 같은 검사로 확인한다. 저장 점수는 null이다.
            if pending and raw["awarded"] is None:
                core_data["awarded"] = 0
            row, problems = check_item(core_data, item, answer, record)
            if pending:
                row["awarded"] = None
            if type(raw["review_required"]) is not bool:
                problems.append("review_required는 참/거짓이어야 함")
            if not isinstance(raw["alternative"], str) or not isinstance(raw["review_reason"], str):
                problems.append("alternative·review_reason은 문자열이어야 함")
            elif pending:
                if raw["awarded"] is not None or not raw["review_reason"].strip():
                    problems.append("검토 보류는 null 점수와 검토 사유가 필요함")
            elif raw["review_reason"].strip():
                problems.append("검토 사유가 있으면 review_required를 true로 표시해야 함")
            row.update({key: raw[key] for key in extra})
            if not problems:
                row["quote_locations"] = quote_locations(raw["quotes"], answer)
        if problems:
            row["awarded"] = None
        row["error"] = "; ".join(problems) or None
        if issue_id is None:
            discretion[doc_id] = row
        else:
            issues.setdefault(issue_id, {"items": []})["items"].append(row)
    return {"issues": issues, "discretion": discretion, "validation_errors": errors}
