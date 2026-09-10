"""Prepare quick-report evidence navigation for Feishu, without I/O or model calls.

The returned plan describes text blocks and links, not Feishu block IDs. The
integration resolves those IDs only after import and all structural formatting.
"""
from __future__ import annotations

from collections import defaultdict
from html.parser import HTMLParser
import re

import markdown


_HEADING = re.compile(r"^(#{2,6})[ \t]+(.+?)\s*$")
_EVIDENCE = re.compile(r"^### \[(E[1-9]\d*)\] (.+)$")
_REFS = re.compile(r"(?<!\\)\[(E[1-9]\d*)\]")
_TRAILING_REFS = re.compile(r"(?<!\\)\[E[1-9]\d*\](?:\s+\[E[1-9]\d*\])*\s*$")


class _InlineText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)


def _plain_text(value: str) -> str:
    parser = _InlineText()
    parser.feed(markdown.markdown(value))
    return "".join(parser.parts).strip()


def _block_line(line: str) -> tuple[str, str]:
    ordered = re.match(r"^\d+\.\s+(.+)$", line)
    bullet = re.match(r"^-\s+(.+)$", line)
    if ordered:
        return "ordered", _plain_text(ordered[1])
    if bullet:
        return "bullet", _plain_text(bullet[1])
    return "text", _plain_text(line)


def prepare_evidence_navigation(md: str) -> tuple[str, dict | None]:
    """Recognize the deterministic quick-report layout; otherwise leave it alone.

    Evidence headings alone are demoted. Unreferenced evidence, quotations,
    tables, scope, risks and limitations remain verbatim in the export copy.
    No temporary tokens or fabricated URLs are inserted into the document.
    """
    lines = md.splitlines()
    # Ignore headings inside fenced source/code material.
    structural = []
    eligible = set()
    fence = ""
    for index, line in enumerate(lines):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if not fence:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = ""
            continue
        if not fence:
            eligible.add(index)
            if _HEADING.match(line):
                structural.append(index)
    positions = []
    for title in ("核心判断", "关键发现", "发现与证据附录"):
        matches = [i for i in structural if lines[i] == f"## {title}"]
        if len(matches) != 1:
            return md, None
        positions.append(matches[0])
    core, findings, appendix = positions
    if not core < findings < appendix:
        return md, None
    evidence = {i: _EVIDENCE.match(lines[i]) for i in structural if i > appendix}
    evidence = {i: match for i, match in evidence.items() if match}
    ids = [match[1] for match in evidence.values()]
    inventory = [i for i in structural if appendix < i and lines[i] == "### 完整发现目录"]
    if not ids or len(set(ids)) != len(ids) or len(inventory) != 1 or inventory[0] >= min(evidence):
        return md, None

    plan: dict = {"headings": [], "nodes": []}
    sections: dict[int, int] = {}
    section = -1
    for index in structural:
        if index in evidence:
            continue
        heading = _HEADING.match(lines[index])
        section += 1
        sections[index] = section
        plan["headings"].append({"kind": f"heading{len(heading[1])}", "text": _plain_text(heading[2])})
    section_at: dict[int, int] = {}
    section = -1
    for index in range(len(lines)):
        section = sections.get(index, section)
        section_at[index] = section

    def add_node(key: str, index: int, line: str, links: list[dict] | None = None):
        kind, text = _block_line(line)
        node = {"key": key, "section": section_at[index], "kind": kind, "text": text, "links": links or []}
        plan["nodes"].append(node)
        return node

    backlinks: dict[str, list[tuple[str, str]]] = defaultdict(list)
    counters: dict[str, int] = defaultdict(int)
    subsection = "核心判断"
    finding_number = 0
    for index in range(core + 1, appendix):
        if index not in eligible:
            continue
        line = lines[index]
        if index in sections:
            if line == "## 关键发现":
                subsection = "关键发现"
            elif line.startswith("### "):
                subsection = _plain_text(line[4:])
                if index > findings:
                    finding_number += 1
            continue
        # Only generated citation-bearing rows are eligible, never quotations or
        # the narrative/statistics lines that happen to contain bracketed text.
        if not (re.match(r"^(?:\d+\. |\- )", line) or (index > findings and line.startswith("证据："))):
            continue
        trailing = _TRAILING_REFS.search(line)
        if not trailing:
            continue
        refs = list(dict.fromkeys(_REFS.findall(trailing[0])))
        if any(ref not in ids for ref in refs):
            return md, None
        label_group = "关键发现" if index > findings else subsection
        counters[label_group] += 1
        number = finding_number if index > findings else counters[label_group]
        label = f"{label_group} {number}"
        key = f"reference:{index}"
        node = add_node(key, index, line)
        # Limit the link spans to the generated suffix, preserving literal text.
        suffix_start = len(node["text"]) - len(trailing[0].rstrip())
        for match in _REFS.finditer(node["text"], max(0, suffix_start)):
            node["links"].append({"start": match.start(), "end": match.end(), "target": f"evidence:{match[1]}"})
        for ref in refs:
            backlinks[ref].append((label, key))

    # The compact inventory is also navigable, including supplementary evidence.
    for index in range(inventory[0] + 1, min(evidence)):
        match = re.match(r"^- \[(E[1-9]\d*)\] ", lines[index])
        if match and match[1] in ids:
            ref = match[1]
            add_node(f"inventory:{ref}", index, lines[index], [{"start": 0, "end": len(ref) + 2, "target": f"evidence:{ref}"}])

    output = []
    for index, line in enumerate(lines):
        match = evidence.get(index)
        if not match:
            output.append(line)
            continue
        ref = match[1]
        title = f"**[{ref}] {match[2]}**"
        add_node(f"evidence:{ref}", index, title)
        output.append(title)
        returns = backlinks[ref]
        if not returns:
            inventory_key = f"inventory:{ref}"
            if any(node["key"] == inventory_key for node in plan["nodes"]):
                returns = [("返回完整发现目录", inventory_key)]
        if returns:
            text = f"返回引用（{ref}）："
            links = []
            for label, target in returns:
                if links:
                    text += " · "
                start = len(text)
                text += label
                links.append({"start": start, "end": len(text), "target": target})
            add_node(f"backlinks:{ref}", index, text, links)
            output.extend(["", text, ""])
    return "\n".join(output), plan
