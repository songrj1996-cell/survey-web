"""Prepare quick-report evidence navigation for Feishu, without I/O or model calls.

The returned plan describes text blocks and links, not Feishu block IDs. The
integration resolves those IDs only after import and all structural formatting.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from html.parser import HTMLParser
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

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


def navigation_document_identity(url: str) -> tuple[str, str]:
    """Validate a document locator, without requesting the user-supplied URL."""
    parsed = urlsplit(url.strip())
    match = re.fullmatch(r"/docx/([A-Za-z0-9_-]{1,128})", parsed.path)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443) or not match
            or not (host.endswith(".feishu.cn") or host.endswith(".larksuite.com"))):
        raise ValueError("expected canonical Feishu docx URL")
    return match[1], urlunsplit(("https", parsed.netloc.lower(), parsed.path, "", ""))


def _rich_block(block: dict) -> tuple[str, list[dict], str]:
    for kind in ("text", "bullet", "ordered", *(f"heading{i}" for i in range(1, 10))):
        if kind in block:
            elements = block[kind].get("elements", [])
            if any(set(element) != {"text_run"} for element in elements):
                return kind, [], ""
            return kind, elements, "".join(element["text_run"].get("content", "") for element in elements)
    return "", [], ""


def _link_groups(elements: list[dict]) -> list[tuple[int, int, str, str]]:
    groups = []
    for index, element in enumerate(elements):
        run = element["text_run"]
        url = run.get("text_element_style", {}).get("link", {}).get("url", "")
        if not url:
            continue
        # Feishu returns both encoded and normalized URL representations.
        normalized = url if url.startswith("https://") else unquote(url)
        if groups and groups[-1][1] == index and groups[-1][2] == normalized:
            start, _, previous, text = groups.pop()
            groups.append((start, index + 1, previous, text + run.get("content", "")))
        else:
            groups.append((index, index + 1, normalized, run.get("content", "")))
    return groups


def prepare_wiki_navigation_updates(
    blocks: list[dict], doc_token: str, doc_url: str, wiki_url: str,
) -> dict:
    """Retarget existing, verifiable evidence links; never reconstruct text.

    The original exported block ID stays the destination. A missing/ambiguous
    destination or a manually repurposed link is skipped, never guessed.
    """
    old, new = urlsplit(doc_url), urlsplit(wiki_url)
    if (old.scheme != "https" or new.scheme != "https" or old.netloc != new.netloc
            or old.path != f"/docx/{doc_token}"
            or not re.fullmatch(r"/wiki/[A-Za-z0-9_-]{1,128}", new.path)
            or new.query or new.fragment or old.username or new.username):
        raise ValueError("wiki navigation identity mismatch")
    by_id = {block.get("block_id"): block for block in blocks}
    if len(by_id) != len(blocks) or None in by_id:
        raise ValueError("ambiguous document block IDs")
    root = [block for block in blocks if block.get("parent_id") == doc_token]
    children = by_id.get(doc_token, {}).get("children")
    if not isinstance(children, list):
        raise ValueError("missing root document block")
    if len(children) != len(set(children)) or set(children) != {b["block_id"] for b in root}:
        raise ValueError("incomplete root document blocks")
    root = [by_id[block_id] for block_id in children]
    rich = [_rich_block(block) for block in root]
    positions = []
    for kind, title in (("heading2", "核心判断"), ("heading2", "关键发现"),
                        ("heading2", "发现与证据附录"), ("heading3", "完整发现目录")):
        matches = [i for i, item in enumerate(rich) if item[0] == kind and item[2].strip() == title]
        if len(matches) != 1:
            raise ValueError("not an exported quick-report document")
        positions.append(matches[0])
    core, findings, appendix, inventory = positions
    if positions != sorted(set(positions)):
        raise ValueError("invalid quick-report section order")
    evidence: dict[str, list[str]] = defaultdict(list)
    for index in range(inventory + 1, len(root)):
        kind, elements, text = rich[index]
        match = re.match(r"^\[(E[1-9]\d*)\]\s+\S", text.strip())
        first = next((element["text_run"] for element in elements
                      if element["text_run"].get("content", "").strip()), {})
        if (kind == "text" and match
                and first.get("text_element_style", {}).get("bold")):
            evidence[match[1]].append(root[index]["block_id"])
    evidence = {ref: ids[0] for ref, ids in evidence.items() if len(ids) == 1}
    if not evidence:
        raise ValueError("missing unambiguous evidence blocks")
    indexes = {block["block_id"]: i for i, block in enumerate(root)}

    def destination(url: str) -> str | None:
        parsed = urlsplit(url)
        if (parsed.scheme == "https" and parsed.netloc == old.netloc
                and parsed.path in (old.path, new.path)):
            return unquote(parsed.fragment)
        return None

    def cites(block_id: str, ref: str) -> bool:
        index = indexes.get(block_id, -1)
        if index < 0:
            return False
        kind, elements, text = rich[index]
        eligible = (core < index < appendix and kind in {"text", "bullet", "ordered"})
        eligible |= index > inventory and kind == "bullet" and text.strip().startswith(f"[{ref}] ")
        return eligible and any(
            label == f"[{ref}]" and destination(url) == evidence.get(ref)
            for _, _, url, label in _link_groups(elements)
        )

    updates, skipped, already, eligible = [], 0, 0, 0
    for index, block in enumerate(root):
        kind, elements, text = rich[index]
        if kind not in {"text", "ordered", "bullet"} or index <= core:
            continue
        backlink = re.match(r"^返回引用（(E[1-9]\d*)）：", text.strip()) if index > inventory else None
        is_source = (index < appendix or (index > inventory and kind == "bullet"))
        if not (backlink or is_source):
            continue
        replacement = deepcopy(elements)
        changed = False
        for start, end, url, label in _link_groups(elements):
            target = destination(url)
            if target is None:  # Other documents and external sources are untouched.
                continue
            eligible += 1
            reference = re.fullmatch(r"\[(E[1-9]\d*)\]", label)
            valid = bool(reference and is_source and evidence.get(reference[1]) == target)
            if backlink:
                valid = backlink[1] in evidence and cites(target, backlink[1])
            if not valid or target not in by_id:
                skipped += 1
                continue
            desired = f"{wiki_url}#{quote(target, safe='')}"
            if url == desired:
                already += 1
                continue
            for offset in range(start, end):
                replacement[offset]["text_run"]["text_element_style"]["link"]["url"] = quote(desired, safe="")
            changed = True
        if changed:
            updates.append({"block_id": block["block_id"], "update_text_elements": {"elements": replacement}})
    if not eligible:
        raise ValueError("missing exported evidence links")
    return {"updates": updates, "skipped_links": skipped, "already_current": already}
