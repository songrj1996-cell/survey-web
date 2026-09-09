"""Quick report evidence contracts. No I/O, source mutation, or model calls."""
from __future__ import annotations

from copy import deepcopy
import html
import json
import re

QUICK_SCHEMA_VERSION = 1
QUICK_CONTRACT = """<quick_report_contract>
只返回一个JSON对象，不用Markdown代码围栏。schema_version必须为1：
{"schema_version":1,"title":"报告标题","core":[{"text":"核心判断及边界","evidence_ids":["E1"]}],
"findings":[{"title":"具体发现","summary":"发现与必要数字","reason":"原因与场景",
"exceptions":"分歧或例外；没有时说明未发现或无法判断","implication":"分析推断及产品含义",
"limitations":"证据边界","evidence_ids":["E1"]}],
"risks":[{"text":"少数高风险反馈及待核实状态","evidence_ids":["E1"]}],
"actions":[{"text":"建议动作、验证方式及前提","evidence_ids":["E1"]}]}
core、findings、actions均不得为空；risks在无风险证据时允许为空。各字段文本只能写普通文本，
不得插入HTML、标题、代码、额外的证据标记或链接。每项的evidence_ids必须非空且全部存在于目录。
不得以篇幅为由删除重大风险、关键人群分歧和证据边界。避免重复展开同一发现。
evidence_catalog包含全部有效主题；未在正文引用的项目由系统完整保留，不需要你再次生成附录。
统计表为确定性结果，player_quotes为原文证据，不代表全部回答；raw_fallback包含未可靠归类的全部原文。
无开放题或证据不足时，应基于现有统计写清能确认什么、缺什么信息及如何补充，不得编造反馈。
</quick_report_contract>"""


def supports_quick_report(source: dict) -> bool:
    return (
        (source.get("mode") or "") not in {"crosstab", "comment", "interview", "annotate"}
        and source.get("analysis_mode") != "quantitative"
    )


def normalize_report_style(value) -> str:
    value = "full" if value is None or value == "" else value
    if value not in ("full", "quick"):
        raise ValueError("报告模式必须是 full 或 quick")
    return value


def _line(value) -> str:
    """Untrusted labels/quotes cannot introduce HTML, headings or citations."""
    text = " ".join(str(value or "").split())
    text = html.escape(text, quote=False)
    return re.sub(r"([\\`*_{}\[\]<>#|])", r"\\\1", text)


def build_evidence_catalog(clustered: dict, viewpoints: list, scopes: list, stats: dict, diagnostics: dict) -> list[dict]:
    """Each source scope and quote stays traceable; no frequency/Top-N filter."""
    catalog = []

    def add(**fields):
        catalog.append({"id": f"E{len(catalog) + 1}", **fields})

    for title, markdown in stats.items():
        if str(markdown or "").strip():
            add(kind="statistics", title=str(title), statistics=str(markdown), player_quotes=[])
    for scope_key, col_idx, part_index, part, entries in scopes:
        data = clustered.get(scope_key) or clustered.get(str(scope_key)) or {}
        diag = diagnostics.get(scope_key) or diagnostics.get(str(scope_key)) or {}
        scope_label = f"Part {part_index} {part.get('name', '')} / {data.get('col_name') or f'题目列{col_idx}'}"
        if data.get("filter_desc"):
            scope_label += f"；筛选范围：{data['filter_desc']}"
        for theme in data.get("all_themes") or data.get("themes") or []:
            quotes = deepcopy(theme.get("quote_evidence") or [
                {"quote": q, "source": {}} for q in theme.get("quotes") or theme.get("source_quotes") or []
            ])
            count = theme.get("count")
            total = data.get("total")
            unit = "名玩家" if data.get("count_unit") == "players" else "条回答"
            basis = (
                f"{count}{unit}提及，占本题{total}{unit}的{theme.get('percentage')}%。"
                if count is not None and total and theme.get("percentage") is not None
                else "该主题没有可用的精确频次统计，不判断频次或排名。"
            )
            add(kind="theme", title=str(theme.get("name") or "未命名主题"),
                source_key=f"QVIEW:{scope_key}:{theme.get('id')}", scope=scope_label,
                description=str(theme.get("description") or ""),
                positive_summary=str(theme.get("positive_summary") or ""),
                negative_summary=str(theme.get("negative_summary") or ""),
                statistics=basis, player_quotes=quotes,
                quality="degraded" if diag.get("quality_status") == "degraded" else "available")
        # Partial extraction/classification failures require the whole source pool,
        # not a representative sample selected from an incomplete theme inventory.
        if entries and (not (data.get("all_themes") or data.get("themes")) or diag.get("status") == "failed" or diag.get("quality_status") == "degraded"):
            add(kind="raw_fallback", title=f"{scope_label}：未可靠归类的原文材料",
                scope=scope_label, statistics="原文兜底：主题覆盖或统计不完整，不据此推算观点频次。",
                player_quotes=[{"quote": str(e.get("text") or ""), "source": {
                    "ids": deepcopy(e.get("ids") or {}), "profile": deepcopy(e.get("profile") or {})
                }} for e in entries if str(e.get("text") or "").strip()], quality="degraded")
    for viewpoint in viewpoints or []:
        add(kind="viewpoint", title=str(viewpoint.get("name") or "跨题观点"),
            source_key=str(viewpoint.get("id") or ""),
            scope="；".join(viewpoint.get("source_questions") or []),
            description=str(viewpoint.get("description") or ""),
            statistics=f"{viewpoint['count']}名玩家提及，占相关题目{viewpoint['denominator']}名有效回答玩家的{viewpoint['percentage']}%。",
            player_quotes=[{"quote": q, "source": {}} for q in viewpoint.get("quotes") or []])
    if not catalog:
        add(kind="unavailable", title="数据边界", statistics="没有可用的统计或开放题证据，只能说明数据缺口。", player_quotes=[])
    return catalog


def build_quick_query(catalog: list[dict], *, context: dict, focus: dict, instruction: str) -> str:
    material = {"business_context": context or {}, "analysis_focus": focus or {},
                "supplement": instruction or "", "evidence_catalog": catalog}
    return json.dumps(material, ensure_ascii=False) + "\n\n" + QUICK_CONTRACT


def parse_quick_draft(text: str, catalog: list[dict]) -> dict:
    try:
        draft = json.loads(text.strip())
    except (ValueError, TypeError) as exc:
        raise ValueError("快速报告不是完整JSON对象") from exc
    if not isinstance(draft, dict) or type(draft.get("schema_version")) is not int or draft["schema_version"] != QUICK_SCHEMA_VERSION:
        raise ValueError("快速报告协议版本无效")
    valid = {item["id"] for item in catalog}

    def text_field(item, field):
        value = item.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"快速报告缺少 {field}")
        if re.search(r"<[^>]*>|\[E\d+\]|(?m:^\s*#{1,6}\s)|```", value):
            raise ValueError(f"{field} 必须是普通文本，引用由 evidence_ids 提供")
    text_field(draft, "title")
    for group in ("core", "findings", "risks", "actions"):
        items = draft.get(group)
        if not isinstance(items, list) or (group != "risks" and not items):
            raise ValueError(f"快速报告缺少 {group}")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f"{group} 项目结构无效")
            fields = ("title", "summary", "reason", "exceptions", "implication", "limitations") if group == "findings" else ("text",)
            for field in fields:
                text_field(item, field)
            refs = item.get("evidence_ids")
            if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in valid for ref in refs):
                raise ValueError(f"{group} 含空引用或不存在的证据编号")
            if len(refs) != len(set(refs)):
                raise ValueError(f"{group} 含重复的证据编号")
    return draft


def render_quick_report(draft: dict, catalog: list[dict]) -> tuple[str, dict]:
    used = {ref for group in ("core", "findings", "risks", "actions") for item in draft[group] for ref in item["evidence_ids"]}
    def refs(item):
        return " ".join(f"[{ref}]" for ref in item["evidence_ids"])
    lines = [f"# {_line(draft['title'])}", "", "## 核心判断", ""]
    for i, item in enumerate(draft["core"], 1):
        lines.append(f"{i}. {_line(item['text'])} {refs(item)}")
    if draft["risks"]:
        lines += ["", "### 少数但需优先核实的反馈", ""]
        lines += [f"- {_line(item['text'])} {refs(item)}" for item in draft["risks"]]
    lines += ["", "### 行动建议", ""]
    lines += [f"{i}. {_line(item['text'])} {refs(item)}" for i, item in enumerate(draft["actions"], 1)]
    lines += ["", "## 关键发现", ""]
    labels = {"summary": "主要发现", "reason": "原因与场景", "exceptions": "分歧或例外", "implication": "分析推断与产品含义", "limitations": "证据边界"}
    for item in draft["findings"]:
        lines += [f"### {_line(item['title'])}", ""]
        lines += [f"- **{label}：** {_line(item[key])}" for key, label in labels.items()]
        lines += [f"证据：{refs(item)}", ""]
    lines += ["## 发现与证据附录", "", "### 完整发现目录", "",
              "目录保留全部已识别主题和可用统计；原文兜底不代表已经完整识别其中的观点。正文未展开不代表没有价值。", ""]
    for entry in catalog:
        placement = "正文已引用" if entry["id"] in used else "补充发现与材料"
        lines.append(f"- [{entry['id']}] {_line(entry['title'])}（{placement}）")
    for entry in catalog:
        lines += ["", f"### [{entry['id']}] {_line(entry['title'])}", ""]
        if entry.get("scope"):
            lines += [f"来源范围：{_line(entry['scope'])}", ""]
        if entry.get("quality") == "degraded":
            lines += ["**分析限制：本范围存在原文兜底，主题覆盖或统计可能不完整。**", ""]
        if entry["kind"] == "statistics":
            # Stats are produced by the existing deterministic statistics renderer.
            statistics = re.sub(r"(?m)^#{1,6}\s+(.+)$", lambda match: f"**{_line(match[1])}**", entry["statistics"])
            statistics = re.sub(r"<metadata>(.*?)</metadata>", lambda match: _line(match[1]), statistics, flags=re.DOTALL)
            lines += [statistics, ""]
        else:
            lines += [_line(entry.get("statistics")), ""]
        for key, label in (("description", "主题说明"), ("positive_summary", "正向反馈"), ("negative_summary", "负向反馈")):
            if entry.get(key):
                lines += [f"{label}：{_line(entry[key])}", ""]
        for quote in entry.get("player_quotes") or []:
            source = quote.get("source") or {}
            if isinstance(source, str):
                lines += [f"> {_line(quote.get('quote'))}", f"来源标识与画像：{_line(source) or '未提供'}", ""]
                continue
            ids = source.get("ids") or {}
            player = " / ".join(str(v) for v in ids.values()) if isinstance(ids, dict) else ""
            profile = source.get("profile") or {}
            profile_text = " / ".join(f"{k}={v}" for k, v in profile.items()) if isinstance(profile, dict) else ""
            lines += [f"> {_line(quote.get('quote'))}", f"来源标识：{_line(player) or '未提供'}；画像：{_line(profile_text) or '未提供'}", ""]
    lines += ["### 阅读与统计口径", "", "引用原文按材料原样保留，正文使用中文。主题可能多选，同一玩家可能提及多个主题，人数和占比不能跨题相加。分析推断与建议需要进一步验证，不代表已证实因果或实际产品效果。"]
    return "\n\n".join(line for line in lines if line), {
        "schema_version": QUICK_SCHEMA_VERSION, "catalog_count": len(catalog),
        "body_referenced_count": len(used), "appendix_count": len(catalog),
        "not_expanded_count": len(catalog) - len(used),
        "body_referenced_ids": [entry["id"] for entry in catalog if entry["id"] in used],
        "appendix_only_ids": [entry["id"] for entry in catalog if entry["id"] not in used],
        "body_not_expanded_ratio": round((len(catalog) - len(used)) / max(1, len(catalog)), 4),
    }
