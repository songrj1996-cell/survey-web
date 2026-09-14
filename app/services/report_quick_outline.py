"""Read-only quick-report outline shared by the browser and every export format.

Use only the selected version's frozen branch rules. Neither report text nor
model inputs/checkpoints are persisted by this presentation projection.
"""
import re

from app.services.report_modes import resolve_report_mode
from app.services.report_quick_mode import _line


_HEADING = re.compile(r"^## ([^\r\n]+)$", re.M)
_CONDITION = re.compile(r"^(.*?)\s*【((?:推定适用于|当前回答分布主要对应)[\s\S]*)】$")


def _indexes(value):
    if not isinstance(value, list) or not value or any(type(i) is not int or i < 0 for i in value):
        return None
    return tuple(sorted(set(value)))


def _sections(snapshot):
    summary = snapshot.get("quick_summary")
    if not isinstance(summary, dict):
        return []
    objective = summary.get("objective_stats") or {}
    if not isinstance(objective, dict):
        return []
    stats, questions = objective.get("sections") or [], summary.get("questions") or []
    if not isinstance(stats, list) or not isinstance(questions, list):
        return []
    entries = [(q, True) for q in stats] + [(q, False) for q in questions]
    if any(not isinstance(q, dict) for q, _ in entries):
        return []
    entries.sort(key=lambda item: item[0].get("source_order")
                 if type(item[0].get("source_order")) is int else float("inf"))
    result = []
    for question, is_objective in entries:
        key = question.get("question_key")
        if not isinstance(key, str) or not re.fullmatch(r"[0-9]{1,10}(?::[0-9]{1,10})*", key):
            return []
        if is_objective:
            first = str(question.get("markdown") or "").strip().splitlines()
            if not first or not first[0].startswith("### "):
                return []
            heading = first[0][4:]
        else:
            label = question.get("question_label") or question.get("question_number")
            heading = (f"{_line(label)} " if label else "") + _line(question.get("question") or "未命名题目")
        result.append({"key": key, "indexes": tuple(int(i) for i in key.split(":")), "heading": heading})
    if len({q["key"] for q in result}) != len(result):
        return []
    return result


def build_quick_outline(snapshot: dict) -> dict | None:
    """Return an order-preserving outline, or None when alignment is uncertain."""
    if resolve_report_mode(snapshot) != "quick":
        return None
    frozen = snapshot.get("input_snapshot")
    if not isinstance(frozen, dict):
        return None
    rules = frozen.get("branch_rules")
    if not isinstance(rules, list) or not rules or any(not isinstance(r, dict) for r in rules):
        return None
    sections = _sections(snapshot)
    # Exact full-sequence alignment also protects edited and legacy Markdown.
    markdown = str(snapshot.get("report_md") or "").replace("\r\n", "\n")
    if re.search(r"^ {0,3}(?:`{3,}|~{3,})", markdown, re.M):
        return None
    headings = list(_HEADING.finditer(markdown))
    if not sections or [h[1] for h in headings] != [q["heading"] for q in sections]:
        return None
    columns = frozen.get("confirmed_columns")
    if not isinstance(columns, list):
        return None
    known_indexes = set()
    for column in columns:
        if isinstance(column, dict):
            indexes = _indexes(column.get("column_indexes") or [column.get("column_index", column.get("index"))])
            known_indexes.update(indexes or ())
    targets = []
    for rule in rules:
        items = rule.get("targets")
        if not isinstance(items, list) or any(not isinstance(t, dict) or not _indexes(t.get("indexes")) for t in items):
            return None
        targets.append([_indexes(t["indexes"]) for t in items])
    all_target_indexes = {i for items in targets for indexes in items for i in indexes}
    assignments = []
    uncertain = set()
    for section in sections:
        overlaps = [i for i, items in enumerate(targets)
                    if any(set(section["indexes"]) & set(indexes) for indexes in items)]
        group = None
        if len(overlaps) == 1:
            index = overlaps[0]
            rule = rules[index]
            parent, options = rule.get("parent_index"), rule.get("allowed_options")
            if (section["indexes"] in targets[index]
                    and set(section["indexes"]).issubset(known_indexes)
                    and type(parent) is int and parent in known_indexes
                    and parent not in all_target_indexes
                    and rule.get("confidence") == "high"
                    and rule.get("source") == "inferred_from_responses"
                    and isinstance(rule.get("parent_name"), str) and rule["parent_name"].strip()
                    and isinstance(options, list) and options
                    and all(isinstance(o, str) and o.strip() for o in options)):
                title = " / ".join(" ".join(o.split()) for o in options)
                parent_name = " ".join(rule["parent_name"].split())
                group = (index, title, f"按「{parent_name}」分组；关系根据回答情况推定。")
        if overlaps and group is None:
            uncertain.add(section["key"])
        assignments.append(group)
    if not any(assignments):
        return None
    groups = []
    seen = set()
    for section, assignment in zip(sections, assignments):
        token = assignment[0] if assignment else None
        if groups and groups[-1]["token"] == token:
            groups[-1]["question_keys"].append(section["key"])
            continue
        title, note = (assignment[1], assignment[2]) if assignment else ("", "")
        if assignment and token in seen:
            title += "（续）"
        seen.add(token)
        groups.append({"token": token, "id": f"quick-group-{len(groups) + 1}",
                       "title": title, "note": note, "question_keys": [section["key"]]})
    # Only the initial, unconditioned block is called common questions. Later
    # unresolved questions stay independent at their original positions.
    if groups[0]["token"] is None and not uncertain.intersection(groups[0]["question_keys"]):
        groups[0]["title"] = "基础信息与共同问题"
    for group in groups:
        group.pop("token")
    return {"schema_version": 1, "question_keys": [q["key"] for q in sections], "groups": groups}


def outline_quick_markdown(snapshot: dict, markdown: str) -> str:
    """Add section headings without changing saved content or answer order."""
    outline = build_quick_outline(snapshot)
    if not outline:
        return markdown
    normalized = markdown.replace("\r\n", "\n")
    headings = list(_HEADING.finditer(normalized))
    sections = _sections(snapshot)
    if [h[1] for h in headings] != [q["heading"] for q in sections]:
        return markdown
    membership = {key: group for group in outline["groups"] for key in group["question_keys"]}
    output, start = [], 0
    for section, heading in zip(sections, headings):
        output.append(normalized[start:heading.start()])
        group = membership[section["key"]]
        if group["title"]:
            if section["key"] == group["question_keys"][0]:
                output.append(f"## {_line(group['title'])}\n\n")
                if group["note"]:
                    output.append(f"{_line(group['note'])}\n\n")
            condition = _CONDITION.fullmatch(heading[1])
            output.append(f"### {condition[1].strip()}\n\n{condition[2]}" if condition else f"### {heading[1]}")
        else:
            output.append(heading[0])
        start = heading.end()
    output.append(normalized[start:])
    return "".join(output)
