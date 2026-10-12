"""Code-owned research inputs. No storage, model calls or changes to legacy stats."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

import survey_stats
from app.services.stats_presentation import _parse_qualitative_stats


RESEARCH_CONTEXT_VERSION = "research-context-v1"
STAT_REF_RE = re.compile(r"\[S:(s\d+-r\d+(?:-c\d+)?)\]")


def complete_stats_tables(stats_md: str, structured_tables: list[dict]) -> list[dict]:
    """Preserve every parsed table including cross tabs, deduping only same scope."""
    _, sections = _parse_qualitative_stats(stats_md)
    tables = []
    seen = set()
    for section in sections:
        for index, table in enumerate(section["tables"]):
            label = str(table.get("label") or "").strip("* \n")
            title = section["title"] + (f" / {label}" if label else "")
            item = {
                "part": "" if section["part"] == "__profile__" else section["part"], "title": title,
                "question_title": section["title"], "table_label": label,
                "table_markdown": table["markdown"],
                "headers": deepcopy(table["headers"]), "rows": deepcopy(table["rows"]),
                "notes": deepcopy(section.get("notes") or []),
                "kind": "cross_tab" if index else "distribution",
            }
            key = (item["part"], title, tuple(table["headers"]), tuple(map(tuple, table["rows"])))
            if key not in seen:
                seen.add(key)
                tables.append(item)
    # Other stats providers need not emit qualitative Markdown headings.
    parsed = {(x["part"], x["question_title"]) for x in tables}
    for item in structured_tables or []:
        if (str(item.get("part") or ""), str(item.get("title") or "")) not in parsed:
            tables.append(deepcopy(item))
    for table in tables:
        table["table_key"] = hashlib.sha256(json.dumps(
            {k: table.get(k) for k in ("part", "title", "table_markdown", "notes")},
            ensure_ascii=False, sort_keys=True,
        ).encode()).hexdigest()
    # A profile overview and its unfiltered Part question can be the same table.
    # Alias only identical content/notes for that profile question, never
    # two independently scoped Part tables or a cross tab with a similar title.
    profiles = [t for t in tables if not t.get("part") and t.get("kind") == "distribution"]
    for table in tables:
        if table.get("kind") != "distribution" or not table.get("part"):
            continue
        for profile in profiles:
            headers = ["选项" if h == "取值" else h for h in table.get("headers") or []]
            profile_headers = ["选项" if h == "取值" else h for h in profile.get("headers") or []]
            fields = ("question_title", "rows", "notes")
            if headers == profile_headers and all(table.get(k) == profile.get(k) for k in fields):
                table["table_key"] = profile["table_key"]
                break
    return tables


def statistic_facts(catalog: list[dict]) -> dict[str, dict]:
    """Bound row/cell facts: code fills source values without recomputing them."""
    facts = {}
    for table in catalog:
        headers, rows = table.get("headers") or [], table.get("rows") or []
        if not headers:
            continue
        for index, row in enumerate(rows, 1):
            if len(row) != len(headers):
                continue
            fact_id = f"{table['id']}-r{index:03d}"
            statement = "；".join(f"{header}：{value}" for header, value in zip(headers, row))
            facts[fact_id] = {
                "id": fact_id, "stat_id": table["id"],
                "scope": table.get("part") or "总体样本",
                "question": table.get("title") or "",
                "statement": f"{table.get('title') or '客观统计'}（{statement}）",
                "notes": deepcopy(table.get("notes") or []),
                "row_cells": [{"column": str(header), "value": str(value)}
                              for header, value in zip(headers, row)],
            }
            for column, (header, value) in enumerate(zip(headers, row), 1):
                cell_id = f"{fact_id}-c{column:03d}"
                facts[cell_id] = {
                    **facts[fact_id], "id": cell_id,
                    "row_fact_id": fact_id, "row_statement": facts[fact_id]["statement"],
                    "column": header, "row_label": str(row[0]),
                    "statement": str(value),
                }
    return facts


def model_statistic_facts(facts: dict[str, dict]) -> list[dict]:
    """Expose each complete row once, with its exact cell references alongside."""
    rows = {key: {**{k: deepcopy(v) for k, v in fact.items() if k != "row_cells"}, "cells": []} for key, fact in facts.items()
            if not fact.get("row_fact_id")}
    orphan_cells = []
    for fact in facts.values():
        parent = rows.get(fact.get("row_fact_id"))
        if parent is not None:
            parent["cells"].append({"id": fact["id"], "column": fact["column"], "value": fact["statement"]})
        elif fact.get("row_fact_id"):
            orphan_cells.append(deepcopy(fact))
    return [*rows.values(), *orphan_cells]


def bound_statistic_facts(facts: dict[str, dict], refs: list[str]) -> dict[str, dict]:
    """Close only selected rows over their original cells; never add another row."""
    bound = {ref: deepcopy(facts[ref]) for ref in dict.fromkeys(refs) if ref in facts}
    for fact in list(bound.values()):
        row_id = fact.get("row_fact_id") or fact["id"]
        cells = fact.get("row_cells") or []
        if not cells:
            continue  # Historical facts without cell metadata remain usable as-is.
        row = {key: deepcopy(value) for key, value in fact.items()
               if key not in {"row_fact_id", "row_statement", "column", "row_label"}}
        row.update(id=row_id, statement=fact.get("row_statement") or fact["statement"])
        bound[row_id] = row
        for index, cell in enumerate(cells, 1):
            cell_id = f"{row_id}-c{index:03d}"
            bound[cell_id] = {
                **deepcopy(row), "id": cell_id, "row_fact_id": row_id,
                "row_statement": row["statement"], "column": cell["column"],
                "row_label": cells[0]["value"], "statement": cell["value"],
            }
    return bound


def expand_stat_references(text: str, facts: dict[str, dict]) -> str:
    return STAT_REF_RE.sub(lambda m: facts[m[1]]["statement"] if m[1] in facts else m[0], text)


def validate_stat_references(text: str, facts: dict[str, dict]) -> str | None:
    refs = STAT_REF_RE.findall(text)
    unknown = next((ref for ref in refs if ref not in facts), None)
    if unknown:
        return f"引用了当前章节未提供的确定性统计：{unknown}"
    if "[S:" in STAT_REF_RE.sub("", text):
        return "统计短引用格式错误，应为 [S:s001-r001]"
    return None


def linked_source_context(source_registry: dict, *, rows: list | None, plan: dict) -> tuple[dict, dict]:
    """Attach same-row objective answers without leaking IDs or joining ambiguous rows."""
    registry = deepcopy(source_registry)
    columns = plan.get("columns") or []
    id_cols = [c for role in ("mlbbid", "id") for c in columns if survey_stats.question_type(c) == role]
    objective = [c for c in columns if survey_stats.question_type(c) == "single_choice"]
    by_key = {}
    for number, row in enumerate((rows or [])[1:], 1):
        key = survey_stats._build_open_text_entry(
            row, "", rows[0], id_cols, [], [], row_number=number,
        )["respondent_key"]
        answers = []
        for column in objective:
            index = column["index"]
            value = survey_stats._format_cell(row[index]) if index < len(row) else ""
            if value.strip():
                value = survey_stats._make_normalizer(column.get("value_aliases"))(value.strip())
                answers.append({"question": column.get("name") or str(rows[0][index]), "answer": value})
        by_key.setdefault(key, []).append(answers)
    linked = ambiguous = 0
    for record in registry.values():
        matches = by_key.get(record.get("respondent_key"), [])
        if len(matches) == 1:
            record["case_context"] = matches[0]
            linked += 1
        elif len(matches) > 1:
            record["case_context"] = []
            record["case_link_status"] = "ambiguous_rows"
            ambiguous += 1
    return registry, {"linked_sources": linked, "ambiguous_sources": ambiguous, "version": RESEARCH_CONTEXT_VERSION}


def coverage_ledger(findings: list[dict], chapters: list[dict]) -> list[dict]:
    coverage = {}
    for chapter in chapters:
        for block in chapter.get("blocks") or []:
            for finding_id in block.get("finding_ids") or []:
                coverage.setdefault(finding_id, []).append(f"{chapter['id']}/{block['id']}")
    return [{
        "finding_id": finding["id"], "preserve_reason": finding.get("preserve_reason") or "",
        "required": bool(finding.get("preserve_reason") or finding.get("kind") == "risk" or finding.get("degraded")),
        "locations": coverage.get(finding["id"], []),
    } for finding in findings]
