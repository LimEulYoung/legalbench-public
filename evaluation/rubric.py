#!/usr/bin/env python3
"""채점 루브릭(YAML) — 검증, 항목 순회, 협의회 채점기준표 형식 마크다운 생성.

검증: 항목 배점 합 = 소제목 배점, 소제목 합 = 공소사실(쟁점) 배점,
      쟁점 배점 합 + 재량 배점 = 문서 배점, 문서 합 = 총점, 항목 id 중복 없음.
      (가점 항목은 합계에서 제외)

사용 예 (프로젝트 루트):
  python3 evaluation/rubric.py data/rubrics/pilot/r14-criminal-record.yaml  # stdout
  python3 evaluation/rubric.py data/rubrics/pilot/r14-criminal-record.yaml -o data/rubrics/pilot/r14-criminal-record.md
  python3 evaluation/rubric.py data/rubrics/*.yaml --check            # 검증만
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import yaml


def pts(x) -> str:
    return f"{x:g}점"


def issues(rb: dict):
    """(문서, 쟁점) 순회."""
    for doc in rb["documents"]:
        for issue in doc["issues"]:
            yield doc, issue


def items(issue: dict) -> list[dict]:
    return [it for sec in issue["sections"] for it in sec["items"]]


def scoring_text(item: dict) -> str:
    """YAML의 점수표를 프롬프트·검토 문서·사람 채점표에 똑같이 표시한다."""
    return "\n".join(f"{value:g}점: {description}" for value, description in item["scoring"].items())


def explanation_text(item: dict) -> str:
    return f"채점 해설: {item['explanation']}" if item.get("explanation") else ""


def reference_excerpts(rb: dict, text: str | None) -> dict[str, list[dict[str, str]]]:
    """기준답안의 제목 경로로 쟁점별 본문을 선택한다. 내용 사본은 YAML에 두지 않는다."""
    selected = {q["id"]: q["reference_sections"] for _, q in issues(rb) if q.get("reference_sections")}
    if not selected:
        return {}
    if text is None:
        raise ValueError("기준답안 발췌에 reference_text가 필요함")
    headings = list(re.finditer(r"(?m)^(#{2,6})[ \t]+([^\n]+?)[ \t]*$", text))
    index, stack, seen = {}, [], set()
    for n, match in enumerate(headings):
        level, title = len(match[1]), match[2].strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        path = " / ".join(title for _, title in stack)
        if path in seen:
            raise ValueError(f"기준답안 제목 경로 중복: {path}")
        seen.add(path)
        end = next((h.start() for h in headings[n + 1:] if len(h[1]) <= level), len(text))
        body = text[match.end():end].strip()
        if body:
            index[path] = {"heading": title, "text": body}
    result = {}
    for iid, paths in selected.items():
        if any(path not in index for path in paths):
            raise ValueError(f"{iid}: 기준답안 제목 경로가 없거나 본문이 비어 있음: {paths}")
        result[iid] = [index[path] for path in paths]
    return result


def valid_score(value, item: dict) -> bool:
    """LLM이 고른 점수가 허용 목록에 있는지만 확인한다. 답안 내용은 판단하지 않는다."""
    return type(value) in (int, float) and value in item["scoring"]


def validate_item(item: dict) -> list[str]:
    iid, maximum = item.get("id"), item.get("points")
    errors = []
    if not isinstance(iid, str) or not iid.strip() or not item.get("criterion"):
        errors.append(f"{iid}: id·criterion이 필요함")
    if type(maximum) not in (int, float) or not math.isfinite(maximum) or maximum <= 0:
        return errors + [f"{iid}: 배점은 양수여야 함"]
    levels = item.get("scoring")
    if not isinstance(levels, dict) or 0 not in levels or maximum not in levels:
        return errors + [f"{iid}: scoring에 0점과 만점 조건이 필요함"]
    for value, description in levels.items():
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
            errors.append(f"{iid}: 허용 점수 오류 {value!r}")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"{iid}: {value}점의 인정 조건이 필요함")
    if "partial" in item:
        errors.append(f"{iid}: partial 대신 scoring에 점수 조건을 한 번만 작성할 것")
    if "accept_label" in item:
        if item["accept_label"] not in {"허용 답안", "채점 유의사항", "중복 감점 방지", "만점 조건"} or not item.get("accept"):
            errors.append(f"{iid}: accept_label에는 안내 내용에 맞는 제목과 accept가 필요함")
    if "explanation" in item and (not isinstance(item["explanation"], str) or not item["explanation"].strip()):
        errors.append(f"{iid}: explanation은 비어 있지 않은 문자열이어야 함")
    return errors


def validate(rb: dict) -> list[str]:
    errs: list[str] = []
    seen: set[str] = set()
    total = 0
    for doc in rb["documents"]:
        discretion = doc.get("discretion")
        if discretion:
            errs.extend(validate_item(discretion))
            if discretion["id"] in seen:
                errs.append(f"항목 id 중복: {discretion['id']}")
            seen.add(discretion["id"])
        doc_sum = discretion["points"] if discretion else 0
        for issue in doc["issues"]:
            if "reference_sections" in issue:
                paths = issue["reference_sections"]
                if (not isinstance(paths, list) or not paths
                        or any(not isinstance(p, str) or not p.strip() for p in paths)):
                    errs.append(f"{issue['id']}: reference_sections에 제목 경로 배열이 필요함")
                elif len(paths) != len(set(paths)):
                    errs.append(f"{issue['id']}: reference_sections 제목 경로 중복")
            issue_sum = 0
            for sec in issue["sections"]:
                sec_sum = 0
                for it in sec["items"]:
                    errs.extend(validate_item(it))
                    if it["id"] in seen:
                        errs.append(f"항목 id 중복: {it['id']}")
                    seen.add(it["id"])
                    if it.get("type") != "bonus":
                        sec_sum += it["points"]
                if sec_sum != sec["points"]:
                    errs.append(f"{issue['id']} ‘{sec['title']}’: 항목 합 {sec_sum} ≠ 배점 {sec['points']}")
                issue_sum += sec["points"]
            if issue_sum != issue["points"]:
                errs.append(f"{issue['id']}: 소제목 합 {issue_sum} ≠ 배점 {issue['points']}")
            doc_sum += issue["points"]
        if doc_sum != doc["points"]:
            errs.append(f"{doc['id']}: 쟁점 합+재량 {doc_sum} ≠ 배점 {doc['points']}")
        total += doc["points"]
    if total != rb["meta"]["total"]:
        errs.append(f"문서 합 {total} ≠ 총점 {rb['meta']['total']}")
    return errs


def source_link(path: str, label: str | None = None, lines=None,
                base: str = "data/rubrics") -> str:
    """저장소 상대 경로를 검토 문서 기준의 링크로 표현한다."""
    target = quote(os.path.relpath(path, base), safe="/.-_")
    if lines:
        target += f"#L{lines[0]}-L{lines[1]}"
        label = f"{label or path} L{lines[0]}–{lines[1]}"
    return f"[{label or path}]({target})"


DEFAULT_ZERO = "만점·부분점수·허용 답안의 조건을 어느 것도 충족하지 못함."
DEFAULT_FULL = "평가기준에 적힌 내용과 필요한 사실 적용을 모두 충족함. 명시된 허용 답안도 같은 점수로 인정함."
DEFAULT_FULL_VARIANTS = {
    DEFAULT_FULL,
    "평가기준의 법리·사실 적용을 모두 충족함. 명시된 허용 답안도 같은 점수로 인정함.",
}


def sheet_positions(rb: dict) -> dict[str, str]:
    """관리용 ID 대신 채점표의 문서·쟁점·항목 번호로 원용 위치를 표시한다."""
    positions = {}
    for dnum, doc in enumerate(rb["documents"]):
        document = "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ"[dnum]
        positions[doc["id"]] = document
        for qnum, issue in enumerate(doc["issues"], 1):
            prefix = f"{document}. {qnum}"
            positions[issue["id"]] = prefix
            for snum, section in enumerate(issue["sections"]):
                part = f". {'가나다라마바사아자차카타파하'[snum]}" if len(issue["sections"]) > 1 else ""
                for inum, item in enumerate(section["items"], 1):
                    positions[item["id"]] = f"{prefix}{part}. ({inum})"
        if doc.get("discretion"):
            positions[doc["discretion"]["id"]] = f"{document}. 문서 재량"
    return positions


def render_grading_sheet(rb: dict, reference_text: str | None = None) -> str:
    """사람이 읽는 채점표: 기준·배점·부분점수와 내용별 채점 안내를 표시한다."""
    m = rb["meta"]
    positions = sheet_positions(rb)
    excerpts = reference_excerpts(rb, reference_text)
    identifiers = re.compile(
        r"(?<![A-Za-z0-9_-])(?:"
        + "|".join(re.escape(k) for k in sorted(positions, key=len, reverse=True))
        + r")(?![A-Za-z0-9_-])"
    )

    def display(value):
        return identifiers.sub(lambda match: positions[match.group()], value)

    def item_lines(item, prefix=""):
        award = f"가점 +{pts(item['points'])}" if item.get("type") == "bonus" else pts(item["points"])
        lines = [f"{prefix}{display(item['criterion'])} **({award})**", ""]
        indent = " " * len(prefix)
        if item.get("explanation"):
            lines += [f"{indent}채점 해설: {display(item['explanation'])}", ""]
        for score, condition in item["scoring"].items():
            if (score == 0 and condition == DEFAULT_ZERO) or (score == item["points"] and condition in DEFAULT_FULL_VARIANTS):
                continue
            label = "부분점수" if 0 < score < item["points"] else "점수 조건"
            lines.append(f"{indent}- {label} **{pts(score)}**: {display(condition)}")
        if item.get("accept"):
            lines.append(f"{indent}- {item.get('accept_label', '허용 답안')}: {display(item['accept'])}")
        if lines[-1]:
            lines.append("")
        return lines

    out = [f"# {m['exam']} {m['subject']} 채점기준표", "",
           f"총 {pts(m['total'])} · v{m.get('version', '0.1')} · {m['status']}", ""]
    if m.get("legal_cutoff"):
        out += [f"법령·판례 기준일: {m['legal_cutoff']} · 문제에서 정한 법령과 적용 조건을 따른다.", ""]
    out += ["각 항목의 본문은 만점 기준이며, 굵게 표시한 점수는 그 항목의 배점이다. "
           "부분점수 조건을 여러 개 충족하면 가장 높은 점수 하나를 부여한다. "
           "아무 조건도 충족하지 못하면 0점으로 한다. "
           "별도로 적은 허용 답안·점수 상한·중복 감점 방지 규칙을 함께 적용한다.", ""]
    if m.get("reference_guide"):
        out += [f"기준답안: `{Path(m['reference_guide']).name}` (같은 폴더)", ""]
    for doc in rb["documents"]:
        out += [f"## {positions[doc['id']]}. {doc['title']} ({pts(doc['points'])})", ""]
        for qnum, issue in enumerate(doc["issues"], 1):
            out += [f"### {qnum}. {issue['title']} ({pts(issue['points'])})", ""]
            for excerpt in excerpts.get(issue["id"], []):
                out += [f"**기준답안 — {excerpt['heading']}**", ""]
                body = re.sub(r"(?m)^#{2,6}\s+(.+)$", r"**\1**", excerpt["text"])
                out += ["> " + line if line else ">" for line in body.splitlines()]
                out.append("")
            if issue["id"] in excerpts:
                out += ["**채점 기준과 점수**", ""]
            for snum, section in enumerate(issue["sections"]):
                if len(issue["sections"]) > 1:
                    out += [f"**{'가나다라마바사아자차카타파하'[snum]}. {section['title']} ({pts(section['points'])})**", ""]
                for inum, item in enumerate(section["items"], 1):
                    out += item_lines(item, f"{inum}. ")
        if doc.get("discretion"):
            out += [f"### 문서 재량 ({pts(doc['discretion']['points'])})", ""]
            out += item_lines(doc["discretion"])
    return "\n".join(out)


def render(rb: dict, *, include_research: bool = False, reference_text: str | None = None) -> str:
    if rb["meta"].get("guidelines") and not include_research:
        return render_grading_sheet(rb, reference_text)
    m = rb["meta"]
    out = [f"# {m['exam']} {m['subject']} — 채점기준 ({m['status']})", ""]
    out += [f"- 총점: {pts(m['total'])} · 작성: {m['drafted_by']} · 검토: {m.get('reviewed_by') or '—'}",
            f"- 루브릭 버전: {m.get('version', 1)}",
            f"- 기록: `{m['record']}`",
            f"- 모범답안: `{m['sources']['model_answer']}`",
            f"- 형식 참고: {m['sources']['format_reference']}", "",
            "**설계 근거와 검토 범위**", ""]
    out += [f"- {r}" for r in m.get("scoring_basis", [])]
    if m.get("guidelines"):
        out += ["", "**함께 읽을 자료**", "",
                f"- {source_link(m['guidelines'], '공통 채점 가이드라인')}",
                f"- {source_link(m['record'], '문제·기록 전문')}"]
        for path in m["sources"]["model_answer"].split(";"):
            out.append(f"- {source_link(path.strip(), Path(path.strip()).name)}")
        out += ["", "**작성 범위**", ""]
        out += [f"- {s}" for s in m.get("scope", [])]
        out += ["", "**배점 개요**", "", "| 문서 | 쟁점 ID | 평가 단위 | 배점 |",
                "|---|---|---|---:|"]
        out += [f"| {d['title']} | `{q['id']}` | {q['title']} | {q['points']} |"
                for d, q in issues(rb)]
    out += ["", "**채점 원칙**", ""]
    out += [f"{i}. {r}" for i, r in enumerate(m["grading_rules"], 1)]
    out.append("")

    review: list[tuple[str, str]] = []
    n_items = 0
    for doc in rb["documents"]:
        out += [f"## {doc['id']}. {doc['title']} ({pts(doc['points'])})", ""]
        for k, issue in enumerate(doc["issues"], 1):
            identifier = f" `[{issue['id']}]`" if issue.get("source_refs") else ""
            out += [f"### {k}. {issue['title']} ({pts(issue['points'])}){identifier}", ""]
            out.append(f"> 예상 결론: {issue['expected']}")
            if issue.get("note"):
                out.append(f"> {'⚠ ' if issue.get('flag') == 'review' else ''}메모: {issue['note']}")
                if issue.get("flag") == "review":
                    review.append((issue["id"], issue["note"]))
            out.append("")
            if issue.get("weight_basis"):
                out += [f"배점 이유: {issue['weight_basis']}", ""]
            if issue.get("practice_refs"):
                out += ["관행 근거: " + ", ".join(issue["practice_refs"]) + " · " +
                        source_link(m["sources"]["practice_evidence"], "관행 근거표"), ""]
            if issue.get("reference_answer"):
                out += [f"기준 해설: {issue['reference_answer']}", ""]
            for sec in issue["sections"]:
                out += [f"**○ {sec['title']} ({pts(sec['points'])})**", ""]
                for it in sec["items"]:
                    n_items += 1
                    head = f"(가점 +{pts(it['points'])})" if it.get("type") == "bonus" else f"({pts(it['points'])})"
                    flag = " ⚠" if it.get("flag") == "review" else ""
                    out.append(f"- {head} {it['criterion']} `[{it['id']}]`{flag}")
                    out += [f"  - **{pts(value)}**: {description}" for value, description in it["scoring"].items()]
                    for key, label in (("accept", it.get("accept_label", "함께 인정")), ("record", "기록"),
                                       ("basis", "근거(검토용 — 채점에 쓰지 않음)"), ("note", "메모")):
                        if it.get(key):
                            out.append(f"  - {label}: {it[key]}")
                    if it.get("flag") == "review":
                        review.append((it["id"], it.get("note", "")))
                out.append("")
            if issue.get("source_refs"):
                out += ["**출처·대조 위치**", ""]
                out += [f"- {source_link(s['path'], s['label'], s['lines'])}"
                        for s in issue["source_refs"]]
                out += ["", "줄 번호는 현재 전사본의 줄 번호다. 기록의 인쇄 쪽수와 다르다.", ""]
        if doc.get("discretion"):
            d = doc["discretion"]
            out += [f"### 문서 재량 ({pts(d['points'])}) `[{d['id']}]`", "", d["criterion"], ""]
            out += [f"- {line}" for line in scoring_text(d).splitlines()]
            out.append("")

    out += ["## 검토가 필요한 항목", ""]
    if review:
        out += [f"- `[{rid}]` {note}" for rid, note in review]
    else:
        out.append("- 없음")
    out += ["", f"<!-- 내용 항목 {n_items}개 · evaluation/rubric.py 로 생성 — 직접 고치지 말고 YAML 을 수정할 것 -->", ""]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="채점 루브릭 검증·렌더링")
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("-o", "--output", type=Path, help="출력 파일 (파일 하나일 때)")
    ap.add_argument("--check", action="store_true", help="검증만")
    args = ap.parse_args()

    failed = False
    for f in args.files:
        rb = yaml.safe_load(f.read_text(encoding="utf-8"))
        errs = validate(rb)
        for e in errs:
            print(f"[rubric] {f.name}: ✗ {e}", file=sys.stderr)
        failed |= bool(errs)
        if not errs:
            print(f"[rubric] {f.name}: ✓ 배점 합계 일치 (총 {rb['meta']['total']}점)", file=sys.stderr)
        if args.check:
            continue
        reference = None
        if any(q.get("reference_sections") for _, q in issues(rb)):
            reference = (Path(__file__).resolve().parents[1] / rb["meta"]["reference_guide"]).read_text(encoding="utf-8")
        md = render(rb, reference_text=reference)
        if args.output and len(args.files) == 1:
            args.output.write_text(md, encoding="utf-8")
        else:
            sys.stdout.write(md)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
