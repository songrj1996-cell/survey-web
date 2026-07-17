"""services/report_engine:定性报告生成引擎。

planner/writer 问询构建、大样本分批定性分析、开放题兜底、统计上下文拼装、QA 取数。
"""
import json
import re

from app.core.config import (
    BATCH_SIZE,
    DIFY_ANALYST_KEY,
    DIFY_CLASSIFY_KEY,
    DIFY_LARGE_ANALYST_KEY,
    DIFY_THEME_EXTRACT_KEY,
    DIFY_THEME_MERGE_KEY,
    OTHER_THEME_PCT,
)
from app.services.branch_logic import branch_rule_for_column, branch_rule_label
from app.services.question_detect import ROLE_LABEL_MAP
from app.storage.prompts import _get_planner_extra, _get_writer_requirements

_QUALITATIVE_CONTEXT_LABELS = [
    ("problem", "这次想解决什么问题"),
    ("background", "当前产品/功能背景"),
    ("target_users", "目标用户"),
    ("key_concerns", "最关心的问题"),
    ("report_usage", "报告准备用在哪里"),
]


def _build_business_context_block(qualitative_context: dict | None, extra_note: str = "") -> str:
    """构造 <business_context> block；无有效字段时返回空字符串（不注入，行为不变）。"""
    if not qualitative_context:
        return ""
    lines = []
    for key, label in _QUALITATIVE_CONTEXT_LABELS:
        val = str(qualitative_context.get(key, "") or "").strip()
        if val:
            lines.append(f"- {label}：{val}")
    if not lines:
        return ""
    note = f"（{extra_note}）" if extra_note else ""
    return (
        "\n\n<business_context>\n"
        f"用户提供的业务背景信息{note}：\n"
        + "\n".join(lines)
        + "\n\n使用规则：若存在这些信息，核心结论与行动建议必须优先围绕其中的核心问题、目标用户、"
        "最关心问题和报告用途组织；但不得把业务背景中没有明示的内容写成事实。"
        "凡是基于问卷结构、玩家反馈或上下文做出的判断，必须明确写出依据；"
        "凡是推测或猜测，必须标注为「推测」或「可能」。"
        + "\n</business_context>"
    )


def _has_business_context(qualitative_context: dict | None) -> bool:
    """判断用户是否填写了有效业务上下文。"""
    if not qualitative_context:
        return False
    return any(str(qualitative_context.get(key, "") or "").strip() for key, _ in _QUALITATIVE_CONTEXT_LABELS)


def _build_branch_logic_block(branch_rules: list[dict] | None) -> str:
    """构造供 Planner/Writer 共用的精简跳转关系上下文。"""
    if not branch_rules:
        return ""
    lines = []
    for rule in branch_rules:
        confidence = "高置信度跳转" if rule.get("confidence") == "high" else "疑似条件关系"
        options = " / ".join(str(option) for option in rule.get("allowed_options") or [])
        targets = []
        for target in rule.get("targets") or []:
            answered = target.get("answered_count")
            suffix = f"（{answered} 条有效回答）" if isinstance(answered, int) else ""
            targets.append(f"「{target.get('name') or '未命名题目'}」{suffix}")
        lines.append(
            f"- [{confidence}]「{rule.get('parent_name') or '前置题'}」选择「{options}」"
            f"（进入分支 {rule.get('eligible_count', '?')} 人）→ {'、'.join(targets)}"
        )
    return (
        "<question_branch_logic>\n"
        "以下关系由全量回答的非空分布与题目结构共同推断：\n"
        + "\n".join(lines)
        + "\n\n严格使用规则：\n"
        "1. 同一父题及其[高置信度跳转]后续题应优先放在同一 Part，形成清晰的父题—分支大纲；不同分支必须分开分析，不得合并回答池或混用分母。只有报告结构确有需要时才拆到不同 Part。\n"
        "2. 分支题结论必须写明适用人群；人数/占比以进入该分支人数或该题有效回答数为分母，不得使用问卷总样本。\n"
        "3. 不得把不同题干、不同使用程度人群的主观反馈直接比较为高低；总体使用程度优先依据父级选择题。\n"
        "4. 对[疑似条件关系]不得声称原表单配置了跳转，只能表述为“当前回答分布主要来自该人群”，但仍应与其他人群分开归纳。\n"
        "5. 未列入此块的题目不要自行猜测跳转关系。\n"
        "</question_branch_logic>"
    )


def _branch_note_for_column(plan: dict, column_index: int) -> str:
    rule = branch_rule_for_column(plan.get("branch_rules"), column_index)
    return branch_rule_label(rule, column_index) if rule else ""


def _question_name_with_branch(name: str, plan: dict, column_index: int) -> str:
    note = _branch_note_for_column(plan, column_index)
    return f"{name}【{note}】" if note else name


def _build_planner_sample(rows: list[list], sample_n: int = 5) -> str:
    if not rows:
        return ""
    headers = rows[0]
    sample = rows[1: 1 + sample_n]

    def esc(s):
        return ("" if s is None else str(s)).replace("|", "\\|").replace("\n", "<br>")

    md = "| " + " | ".join(esc(h) for h in headers) + " |\n"
    md += "| " + " | ".join(["---"] * len(headers)) + " |\n"
    for r in sample:
        cells = [r[i] if i < len(r) else "" for i in range(len(headers))]
        md += "| " + " | ".join(esc(c) for c in cells) + " |\n"

    total_data_rows = max(0, len(rows) - 1)
    return (
        f"<sample>\n"
        f"总数据行数（不含表头）: {total_data_rows}\n"
        f"以下展示表头 + 前 {len(sample)} 行样本：\n\n"
        f"{md}\n"
        f"</sample>"
    )


def _build_planner_query_with_confirmed(
    rows: list[list],
    confirmed_columns: list[dict],
    qualitative_context: dict | None = None,
    branch_rules: list[dict] | None = None,
) -> str:
    """构建给 Planner 的完整 query，含用户确认的题型（逻辑题，矩阵题跨多列）。"""
    sample_md = _build_planner_sample(rows)

    confirmed_lines = []
    for q in confirmed_columns:
        # 兼容旧结构（confirmed_type/index）与新结构（role/name_zh/column_indexes）
        role = q.get("role") or q.get("confirmed_type") or "single_choice"
        name = q.get("name_zh") or q.get("name") or "?"
        cis = q.get("column_indexes") or ([q["index"]] if "index" in q else [])
        label = ROLE_LABEL_MAP.get(role, role)
        extra = ""
        if role in ("single_choice", "profile_dim", "multi_choice", "matrix_multi") and q.get("options"):
            opts = "、".join(str(o) for o in q["options"][:12])
            extra += f"，选项: {opts}"
        if role in ("multi_choice",) and q.get("delimiter"):
            extra += f"，分隔符: 「{q['delimiter']}」"
        if role in ("scale", "matrix_scale") and q.get("scale_min") is not None:
            extra += f"，量程: {q.get('scale_min')}–{q.get('scale_max')}"

        if role in ("matrix_scale", "matrix_multi"):
            rows_lbl = "、".join(str(r) for r in (q.get("rows") or []))
            confirmed_lines.append(
                f"- 矩阵题「{name}」({label})，子项行: {rows_lbl}；"
                f"对应列号 {cis}（这些列同属一道题，**必须整体归入同一个 part**）{extra}"
            )
        else:
            idx = cis[0] if cis else q.get("index", 0)
            confirmed_lines.append(f"- 列{idx}「{name}」: {label}{extra}")

    confirmed_block = "<confirmed_column_types>\n" + "\n".join(confirmed_lines) + "\n</confirmed_column_types>"
    extra_instructions = _get_planner_extra()

    # 检测是否存在画像维度列，生成对应的画像约束指令
    profile_dims = [q for q in confirmed_columns if (q.get("role") or q.get("confirmed_type")) == "profile_dim"]
    if not profile_dims:
        profile_constraint = (
            "\n⚠️ 画像约束（严格执行）：本问卷中用户**没有将任何题目标注为画像维度**。\n"
            "- cross_tabs 数组**必须为空** []\n"
            "- open_questions **不得**建议将任何题目用作用户画像或分组维度\n"
            "- 报告不应包含任何「用户画像」/「人群结构」分析章节\n"
        )
    else:
        dim_names = "、".join(
            f"「{q.get('name_zh') or q.get('name') or '?'}」" for q in profile_dims
        )
        profile_constraint = (
            f"\n画像维度约束：本问卷的画像维度列为 {dim_names}。"
            f"cross_tabs 的 profile_index **只能**使用上述列对应的列号，不得使用其他单选题做交叉分析。\n"
        )

    return (
        f"{sample_md}\n\n"
        f"{confirmed_block}\n\n"
        f"重要：以上题型和选项已由用户在界面中逐一确认；选择题选项必须以 <confirmed_column_types> 中的「选项」为权威，不得根据题干、表头或样本重新猜测选项，也不得围绕已确认选项再次提问。\n"
        f"注意：以上题型已由用户在界面中逐一确认，**不得**在 open_questions 中再次对题型进行发问。"
        f"选项的归并方式（哪个原始值归入哪个标准选项）同样已由用户在界面中逐一确认，**不得**在 open_questions 中就选项归并或分拆方式再次提问。"
        f"矩阵题的多个列号务必整体归入同一个 part。"
        f"{profile_constraint}\n"
        f"{_build_branch_logic_block(branch_rules)}\n"
        f"{extra_instructions}"
        f"{_build_business_context_block(qualitative_context, '用于辅助规划章节结构和分析重点')}"
    )


def _build_plan_revision_query(
    plan: dict,
    headers: list[str],
    confirmed_columns: list[dict],
    user_text: str,
    qualitative_context: dict | None = None,
    branch_rules: list[dict] | None = None,
) -> str:
    header_lines = "\n".join(f"- 列{i}: {h}" for i, h in enumerate(headers))
    confirmed_json = json.dumps(confirmed_columns or [], ensure_ascii=False, indent=2)
    plan_json = json.dumps(plan or {}, ensure_ascii=False, indent=2)
    return (
        "你正在修订一份问卷分析方案。请根据用户的修改意见，在当前方案基础上输出一份完整的新 plan JSON。\n\n"
        "严格要求：\n"
        "1. 只能输出一个完整 JSON 对象，不要输出解释、确认语、Markdown 文本或 ```json 围栏外的内容。\n"
        "2. JSON 必须包含 columns、parts、cross_tabs、open_questions 字段，并通过既有 schema 校验。\n"
        "3. columns 必须保留用户已确认的题型、列号、选项、矩阵题分组等权威信息；不要重新猜测题型或选项。\n"
        "4. parts 必须使用实际存在的列号；矩阵题成员列必须整体归入同一个 part。\n"
        "5. 若用户意见只要求调整章节/分析重点，只改 parts、cross_tabs 或 open_questions，不要无故改 columns。\n\n"
        f"<headers>\n{header_lines}\n</headers>\n\n"
        f"<confirmed_columns_json>\n{confirmed_json}\n</confirmed_columns_json>\n\n"
        f"<current_plan_json>\n{plan_json}\n</current_plan_json>\n\n"
        f"{_build_branch_logic_block(branch_rules)}\n\n"
        f"<user_revision_request>\n{user_text.strip()}\n</user_revision_request>"
        f"{_build_business_context_block(qualitative_context, '用于辅助判断调整章节/分析重点')}\n\n"
        "请现在返回修订后的完整 JSON 对象。"
    )


def _build_crosstab_planner_query(
    questionnaire_text: str,
    available_questions: list[str],
    open_question_names: list[str],
) -> str:
    """跑数表模式：给章节策划 planner 的初始 query（任务/输出格式由 Dify 应用 system prompt 定义）。"""
    q_text = (questionnaire_text or "").strip()
    if len(q_text) > 12000:
        q_text = q_text[:12000] + "\n…（问卷过长，已截断）"
    avail = "\n".join(f"- {q}" for q in available_questions) or "（无）"
    opens = "\n".join(f"- {q}" for q in open_question_names) or "（无）"
    return (
        f"<questionnaire>\n{q_text}\n</questionnaire>\n\n"
        f"<available_questions>\n{avail}\n</available_questions>\n\n"
        f"<open_questions_list>\n{opens}\n</open_questions_list>\n\n"
        "请基于以上规划报告章节大纲，按 system prompt 约定的 JSON 格式输出。"
    )


def _build_crosstab_plan_revision_query(
    questionnaire_text: str,
    current_parts: list[dict],
    user_text: str,
) -> str:
    """跑数表模式：章节大纲的修订 query。"""
    q_text = (questionnaire_text or "").strip()
    if len(q_text) > 12000:
        q_text = q_text[:12000] + "\n…（问卷过长，已截断）"
    outline = json.dumps(current_parts or [], ensure_ascii=False, indent=2)
    return (
        f"<questionnaire>\n{q_text}\n</questionnaire>\n\n"
        f"<current_outline>\n{outline}\n</current_outline>\n\n"
        f"<user_request>\n{user_text.strip()}\n</user_request>\n\n"
        "请在当前大纲基础上按用户意见调整，按 system prompt 约定的 JSON 格式输出。"
    )


def _render_crosstab_plan_card(plan: dict) -> str:
    """跑数表模式：把章节大纲 + 待确认问题渲染成给用户/历史看的 markdown。"""
    lines = ["## 报告章节大纲"]
    for i, p in enumerate(plan.get("parts", []), 1):
        scope = p.get("scope", "")
        lines.append(f"{i}. **{p['name']}**" + (f" — {scope}" if scope else ""))
    oqs = plan.get("open_questions") or []
    if oqs:
        lines.append("")
        lines.append("## 待确认问题")
        for q in oqs:
            lines.append(f"- {q}")
    return "\n".join(lines)


def _json_loads_loose(raw: str) -> tuple[dict | None, str]:
    raw = (raw or "").strip()
    if not raw:
        return None, "empty"
    candidates = [raw]
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S | re.I)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    brace_start = raw.find("{")
    brace_end = raw.rfind("}")
    if 0 <= brace_start < brace_end:
        candidates.append(raw[brace_start:brace_end + 1])

    for text in candidates:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj, ""
            return None, f"json root is {type(obj).__name__}"
        except Exception as e:
            last_err = str(e)
    return None, last_err[:180] if "last_err" in locals() else "invalid json"


def _cluster_diag_column(col_idx: int, col_name: str, total: int, batches: int) -> dict:
    return {
        "col_index": col_idx,
        "col_name": col_name,
        "total": total,
        "batches": batches,
        "phase_a": [],
        "phase_b": {},
        "phase_c": [],
        "status": "running",
        "reason": "",
        "themes": 0,
        "classifications": 0,
        "assignments": 0,
    }


async def _batch_qualitative_analysis(
    open_text: dict,
    plan: dict,
    headers: list,
    session_id: str,
):
    """大样本定性分析四阶段批处理。

    异步生成器，yield ("progress", msg) 或 ("result", clustered_themes)。
    clustered_themes 结构：
    {
        col_idx: {
            "col_name": str,
            "total": int,
            "themes": [{"id","name","description","count","percentage",
                        "positive_count","positive_pct","positive_summary",
                        "negative_count","negative_pct","negative_summary",
                        "quotes": [str]}],
            "other_themes": [{"name","count","percentage"}]
        }
    }
    """
    import json as _json
    from app.integrations.dify_client import workflow_run, STOP_SIGNAL

    clustered_themes: dict = {}
    diagnostics: dict[str, dict] = {}

    for col_idx, entries in open_text.items():
        col = next((c for c in plan["columns"] if c["index"] == col_idx), None)
        col_name = (col and col.get("name")) or (
            headers[col_idx] if col_idx < len(headers) else f"列{col_idx}"
        )
        col_name = f"{col_name}{_open_text_source_note(entries)}"
        col_name = _question_name_with_branch(col_name, plan, col_idx)
        total = len(entries)
        yield ("progress", f"【{col_name}】开始分析（共 {total} 条）")

        # ── Phase A：分批提取主题候选 ──────────────────────────────────────
        batches = [entries[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
        all_candidates: list[dict] = []
        diag = _cluster_diag_column(col_idx, col_name, total, len(batches))
        diagnostics[str(col_idx)] = diag

        for bi, batch in enumerate(batches, 1):
            yield ("progress", f"【{col_name}】提取主题（批次 {bi}/{len(batches)}）")
            responses_text = "\n".join(
                f"[{i}] {e.get('text', '')}" for i, e in enumerate(batch)
            )
            raw = await workflow_run(
                inputs={
                    "question": col_name,
                    "responses": responses_text,
                    "count": len(batch),
                },
                api_key=DIFY_THEME_EXTRACT_KEY,
                log_prefix=f"A col={col_idx} batch={bi}",
            )
            phase_a = {"batch": bi, "raw_len": len(raw or ""), "parsed": False, "themes": 0, "error": ""}
            if raw == STOP_SIGNAL:
                phase_a["error"] = "Dify returned 400 / STOP_SIGNAL"
                diag["phase_a"].append(phase_a)
                diag["status"] = "failed"
                diag["reason"] = phase_a["error"]
                yield ("progress", f"【{col_name}】主题提取遇到错误，跳过该列")
                break
            parsed, err = _json_loads_loose(raw)
            if parsed:
                themes = parsed.get("themes", [])
                if isinstance(themes, list):
                    all_candidates.extend(themes)
                    phase_a["parsed"] = True
                    phase_a["themes"] = len(themes)
                else:
                    phase_a["error"] = "`themes` is not list"
            else:
                phase_a["error"] = err
                yield ("progress", f"【{col_name}】主题提取结果解析失败（批次 {bi}），继续处理后续批次")
            diag["phase_a"].append(phase_a)

        if not all_candidates:
            diag["status"] = "failed"
            diag["reason"] = diag["reason"] or "主题提取未返回 themes"
            yield ("progress", f"【{col_name}】没有提取到主题，后续报告将尝试使用原文兜底")
            continue

        # ── Phase B：合并去重 ──────────────────────────────────────────────
        yield ("progress", f"【{col_name}】合并主题候选（共 {len(all_candidates)} 个）")
        candidates_text = "\n".join(
            f"- {t.get('name', '')}：{t.get('description', '')}"
            for t in all_candidates
        )
        raw_b = await workflow_run(
            inputs={
                "question": col_name,
                "theme_candidates": candidates_text,
                "total_responses": total,
            },
            api_key=DIFY_THEME_MERGE_KEY,
            log_prefix=f"B col={col_idx}",
        )
        diag["phase_b"] = {"raw_len": len(raw_b or ""), "parsed": False, "themes": 0, "error": ""}
        if raw_b == STOP_SIGNAL or not raw_b:
            diag["phase_b"]["error"] = "Dify returned STOP_SIGNAL" if raw_b == STOP_SIGNAL else "empty response"
            diag["status"] = "failed"
            diag["reason"] = f"主题合并失败：{diag['phase_b']['error']}"
            yield ("progress", f"【{col_name}】主题合并失败，跳过该列")
            continue
        merged, err = _json_loads_loose(raw_b)
        if not merged:
            diag["phase_b"]["error"] = err
            diag["status"] = "failed"
            diag["reason"] = f"主题合并结果解析失败：{err}"
            yield ("progress", f"【{col_name}】主题合并结果解析失败，跳过该列")
            continue
        final_themes = merged.get("themes", [])
        if isinstance(final_themes, list):
            diag["phase_b"]["parsed"] = True
            diag["phase_b"]["themes"] = len(final_themes)
            diag["themes"] = len(final_themes)
        else:
            final_themes = []
            diag["phase_b"]["error"] = "`themes` is not list"

        if not final_themes:
            diag["status"] = "failed"
            diag["reason"] = diag["reason"] or "主题合并未返回 themes"
            yield ("progress", f"【{col_name}】主题合并为空，后续报告将尝试使用原文兜底")
            continue

        theme_list_text = _json.dumps(
            [{"id": t["id"], "name": t["name"], "description": t["description"]}
             for t in final_themes],
            ensure_ascii=False,
        )

        # ── Phase C：回跑分类 ──────────────────────────────────────────────
        # counts[theme_id] = {"total": int, "pos": int, "neg": int, "neutral": int, "mixed": int}
        counts: dict[str, dict] = {t["id"]: {"total": 0, "pos": 0, "neg": 0, "neutral": 0, "mixed": 0}
                                    for t in final_themes}
        counts["other"] = {"total": 0, "pos": 0, "neg": 0, "neutral": 0, "mixed": 0}
        # quotes_pool[theme_id] = list of (sentiment, text)
        quotes_pool: dict[str, list] = {t["id"]: [] for t in final_themes}

        for bi, batch in enumerate(batches, 1):
            yield ("progress", f"【{col_name}】分类回复（批次 {bi}/{len(batches)}）")
            responses_text = "\n".join(
                f"[{i}] {e.get('text', '')}" for i, e in enumerate(batch)
            )
            raw_c = await workflow_run(
                inputs={
                    "question": col_name,
                    "theme_list": theme_list_text,
                    "responses": responses_text,
                },
                api_key=DIFY_CLASSIFY_KEY,
                log_prefix=f"C col={col_idx} batch={bi}",
            )
            phase_c = {"batch": bi, "raw_len": len(raw_c or ""), "parsed": False, "classifications": 0, "assignments": 0, "error": ""}
            if raw_c == STOP_SIGNAL:
                phase_c["error"] = "Dify returned 400 / STOP_SIGNAL"
                diag["phase_c"].append(phase_c)
                yield ("progress", f"【{col_name}】分类回复遇到错误（批次 {bi}），继续处理后续批次")
                continue
            cls_data, err = _json_loads_loose(raw_c)
            if not cls_data:
                phase_c["error"] = err
                diag["phase_c"].append(phase_c)
                yield ("progress", f"【{col_name}】分类结果解析失败（批次 {bi}），继续处理后续批次")
                continue

            classifications = cls_data.get("classifications", [])
            if not isinstance(classifications, list):
                phase_c["error"] = "`classifications` is not list"
                diag["phase_c"].append(phase_c)
                continue
            phase_c["parsed"] = True
            phase_c["classifications"] = len(classifications)
            diag["classifications"] += len(classifications)

            for item in classifications:
                try:
                    resp_idx = int(str(item.get("response_id", "")).strip("[]"))
                    original_text = batch[resp_idx].get("text", "") if resp_idx < len(batch) else ""
                except (ValueError, IndexError):
                    continue

                assignments = item.get("assignments", [])
                if isinstance(assignments, list):
                    phase_c["assignments"] += len(assignments)
                for assign in assignments:
                    tid = assign.get("theme_id", "other")
                    sentiment = assign.get("sentiment", "neutral")
                    if tid not in counts:
                        tid = "other"
                    counts[tid]["total"] += 1
                    if sentiment == "positive":
                        counts[tid]["pos"] += 1
                    elif sentiment == "negative":
                        counts[tid]["neg"] += 1
                    elif sentiment == "mixed":
                        counts[tid]["mixed"] += 1
                    else:
                        counts[tid]["neutral"] += 1
                    if tid != "other" and len(quotes_pool[tid]) < 10 and original_text:
                        quotes_pool[tid].append((sentiment, original_text))
            diag["assignments"] += phase_c["assignments"]
            diag["phase_c"].append(phase_c)

        # ── 统计汇总 ──────────────────────────────────────────────────────
        total_mentions = sum(v["total"] for v in counts.values())
        if total_mentions == 0:
            diag["status"] = "failed"
            diag["reason"] = "分类阶段未产生任何主题归属"
            yield ("progress", f"【{col_name}】分类未产生有效归属，后续报告将尝试使用原文兜底")
            continue

        themes_out = []
        other_themes_out = []

        for t in final_themes:
            tid = t["id"]
            c = counts[tid]
            cnt = c["total"]
            pct = round(cnt / total_mentions * 100, 1)
            pos_cnt = c["pos"]
            neg_cnt = c["neg"]
            pos_pct = round(pos_cnt / cnt * 100, 1) if cnt else 0.0
            neg_pct = round(neg_cnt / cnt * 100, 1) if cnt else 0.0

            # 代表性引用：每种情感最多取 1-2 条
            pool = quotes_pool.get(tid, [])
            pos_q = [txt for sent, txt in pool if sent == "positive"][:2]
            neg_q = [txt for sent, txt in pool if sent == "negative"][:2]
            neu_q = [txt for sent, txt in pool if sent not in ("positive", "negative")][:2]
            quotes = (pos_q + neg_q + neu_q)[:6]
            # 不足 3 条时从 pool 中补充未使用的原文，保证 Writer 有足够素材
            if len(quotes) < 3:
                used = set(quotes)
                extras = [txt for _, txt in pool if txt not in used]
                quotes = quotes + extras[:max(0, 3 - len(quotes))]

            entry = {
                "id": tid,
                "name": t["name"],
                "description": t.get("description", ""),
                "count": cnt,
                "percentage": pct,
                "positive_count": pos_cnt,
                "positive_pct": pos_pct,
                "positive_summary": t.get("positive_summary") or "",
                "negative_count": neg_cnt,
                "negative_pct": neg_pct,
                "negative_summary": t.get("negative_summary") or "",
                "quotes": quotes,
            }
            if pct < OTHER_THEME_PCT:
                other_themes_out.append({"name": t["name"], "count": cnt, "percentage": pct})
            else:
                themes_out.append(entry)

        themes_out.sort(key=lambda x: x["count"], reverse=True)
        other_themes_out.sort(key=lambda x: x["count"], reverse=True)

        clustered_themes[col_idx] = {
            "col_name": col_name,
            "total": total,
            "themes": themes_out,
            "other_themes": other_themes_out,
        }
        diag["status"] = "ok"
        diag["reason"] = ""
        yield ("progress", f"【{col_name}】分析完成，识别 {len(themes_out)} 个主要主题")

    yield ("diagnostics", diagnostics)
    yield ("result", clustered_themes)


def _extract_satisfaction_stats(stats_md: str) -> str:
    """Extract ## sections whose title contains '满意度' from stats markdown."""
    lines = stats_md.split("\n")
    sections: list[list[str]] = []
    current: list[str] = []
    capturing = False

    for line in lines:
        if line.startswith("## "):
            if capturing and current:
                sections.append(current)
            current = [line]
            capturing = "满意度" in line
        elif capturing:
            current.append(line)

    if capturing and current:
        sections.append(current)

    return "\n\n".join("\n".join(s) for s in sections)


def _entry_identity(entry: dict) -> str:
    parts = []
    ids = entry.get("ids") or {}
    profile = entry.get("profile") or {}
    for k, v in ids.items():
        if str(v).strip():
            parts.append(f"{k}={v}")
    for k, v in profile.items():
        if str(v).strip():
            parts.append(f"{k}={v}")
    return "；".join(parts)


def _clip_text(text: str, limit: int = 420) -> str:
    text = str(text or "").strip().replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _sample_open_entries(entries: list[dict], limit: int = 60) -> list[dict]:
    if len(entries) <= limit:
        return entries
    if limit <= 1:
        return entries[:1]
    step = (len(entries) - 1) / (limit - 1)
    idxs = []
    seen = set()
    for i in range(limit):
        idx = round(i * step)
        if idx not in seen:
            seen.add(idx)
            idxs.append(idx)
    return [entries[i] for i in idxs]


def _open_text_source_note(entries: list[dict]) -> str:
    if any(e.get("source") == "choice_other_text" for e in entries or []):
        return "（选择题 Other 填空补充）"
    return ""


def _build_open_text_fallback_md(
    open_text: dict | None,
    clustered_themes: dict,
    plan: dict,
    headers: list[str],
) -> str:
    """Build a deterministic raw-text fallback for open columns without themes."""
    if not open_text:
        return ""

    clustered_keys = {str(k) for k in (clustered_themes or {}).keys()}
    blocks = []
    total_chars = 0
    max_chars = 45000

    for raw_idx, entries in open_text.items():
        idx_key = str(raw_idx)
        if idx_key in clustered_keys:
            continue
        if not entries:
            continue
        try:
            col_idx = int(raw_idx)
        except (TypeError, ValueError):
            col_idx = raw_idx
        col = next((c for c in plan.get("columns", []) if c.get("index") == col_idx), None)
        name = (col and col.get("name")) or (
            headers[col_idx] if isinstance(col_idx, int) and col_idx < len(headers) else f"列{raw_idx}"
        )
        if isinstance(col_idx, int):
            name = _question_name_with_branch(name, plan, col_idx)
        lines = [f"### {name}（列 {raw_idx}，共 {len(entries)} 条非空回答；以下为抽样原文）"]
        name = f"{name}{_open_text_source_note(entries)}"
        lines[0] = f"### {name} (col {raw_idx}, {len(entries)} responses; sampled raw text)"
        for i, entry in enumerate(_sample_open_entries(entries), 1):
            ident = _entry_identity(entry)
            prefix = f"{i}. "
            if ident:
                prefix += f"[{ident}] "
            lines.append(prefix + _clip_text(entry.get("text", "")))
        block = "\n".join(lines)
        if total_chars + len(block) > max_chars:
            blocks.append("### 其余开放题\n（原文较多，已达到兜底上下文上限，未继续展开。）")
            break
        blocks.append(block)
        total_chars += len(block)

    if not blocks:
        return ""
    return (
        "<open_text_fallback>\n"
        "以下开放题未能产出稳定聚类结果。请仅基于这些真实原文做定性归纳和代表性引用；"
        "不要编造精确主题占比或人数。若内容明显属于年龄、性别、地区等画像补充项，不要当作体验观点展开。\n\n"
        + "\n\n".join(blocks)
        + "\n</open_text_fallback>"
    )


def _build_large_sample_writer_query(
    stats_md: str,
    clustered_themes: dict,
    plan: dict,
    headers: list[str],
    open_text: dict | None = None,
    qualitative_context: dict | None = None,
) -> str:
    parts_lines = ["  Part 1 受访者画像（固定）"]
    for i, p in enumerate(plan["parts"], 2):
        if "column_indexes" in p:
            col_names = []
            for idx in p["column_indexes"]:
                col = next((c for c in plan["columns"] if c["index"] == idx), None)
                nm = (col and col.get("name")) or (headers[idx] if idx < len(headers) else f"列{idx}")
                rl = col["role"] if col else "?"
                col_names.append(f"{nm}({rl})")
            parts_lines.append(f"  Part {i} {p['name']}: {'; '.join(col_names)}")
        else:
            scope = p.get("scope", "")
            parts_lines.append(f"  Part {i} {p['name']}" + (f": {scope}" if scope else ""))
    plan_summary = "<plan>\n报告结构：\n" + "\n".join(parts_lines) + "\n</plan>"
    branch_logic_block = _build_branch_logic_block(plan.get("branch_rules"))

    theme_blocks = []
    for col_idx, data in clustered_themes.items():
        col_name = data["col_name"]
        total = data["total"]
        lines = [f"### 问题：{col_name}（共 {total:,} 条有效回答）\n"]

        for i, t in enumerate(data["themes"], 1):
            lines.append(f"**主题{i}：{t['name']}**（提及 {t['count']:,} 人次，占 {t['percentage']}%）")
            if t["positive_summary"] or t["positive_count"]:
                lines.append(f"- 正面（{t['positive_count']:,} / {t['positive_pct']}%）：{t['positive_summary']}")
            if t["negative_summary"] or t["negative_count"]:
                lines.append(f"- 负面（{t['negative_count']:,} / {t['negative_pct']}%）：{t['negative_summary']}")
            if t["quotes"]:
                lines.append(f"- 代表原文（请在报告中完整引用，并附中文翻译）：")
                for q in t["quotes"]:
                    lines.append(f'  > "{q}"')
            lines.append("")

        if data["other_themes"]:
            other_parts = "、".join(
                f"{o['name']}（{o['percentage']}%）" for o in data["other_themes"]
            )
            lines.append(f"**其他声音**（合计占比较低）：{other_parts}")

        theme_blocks.append("\n".join(lines))

    open_text_md = (
        "<open_text_themes>\n" + "\n\n".join(theme_blocks) + "\n</open_text_themes>"
        if theme_blocks else "<open_text_themes>（无开放题聚类结果）</open_text_themes>"
    )
    fallback_md = _build_open_text_fallback_md(open_text, clustered_themes, plan, headers)

    satisfaction_md = _extract_satisfaction_stats(stats_md)
    priority_block = (
        f"<priority_metrics>\n{satisfaction_md}\n</priority_metrics>\n\n"
        if satisfaction_md else ""
    )
    has_context = _has_business_context(qualitative_context)
    requirements = _get_large_sample_writer_requirements(
        has_satisfaction=bool(satisfaction_md),
        has_business_context=has_context,
    )

    return (
        "**任务**：基于以下大样本问卷分析结果撰写调研报告。\n\n"
        + f"{plan_summary}\n\n"
        + (f"{branch_logic_block}\n\n" if branch_logic_block else "")
        + f"<stats>\n{stats_md}\n</stats>\n\n"
        + f"{priority_block}"
        + f"{open_text_md}\n\n"
        + (f"{fallback_md}\n\n" if fallback_md else "")
        + f"**要求**：\n{requirements}"
        + (
            "\n- 必须执行 `<question_branch_logic>`：分支题按适用人群分别归纳，"
            "不得合并不同分支的回答池或使用问卷总样本作为分母。"
            if branch_logic_block else ""
        )
        + _build_business_context_block(qualitative_context, "用于辅助分析重点和建议方向")
    )


def _get_large_sample_writer_requirements(
    has_satisfaction: bool = False,
    has_business_context: bool = False,
) -> str:
    satisfaction_rule = (
        "   - **满意度优先原则**：`<priority_metrics>` 中已提取满意度数据，必须将其作为核心结论中最靠前的 1-2 条展示，须包含具体数字"
        if has_satisfaction else
        "   - **满意度优先原则**：若报告中存在任何与满意度评分/评价相关的数据（如好评率、满意度评分、认可度等），必须将其作为核心结论中最靠前的 1-2 条展示，且须包含具体数字"
    )
    context_rule = (
        "- 用户已提供 `<business_context>` 时，核心结论必须优先回答其中的核心问题，并纳入会影响决策的相关 topic、风险、样本限制；不要按 Part 机械复述。\n"
        if has_business_context else
        "- 用户未提供 `<business_context>` 时，不得编造业务目标；只能根据问卷题目、统计结果和玩家反馈归纳基础发现。如需判断调研意图，必须写成「从问卷内容推测/看起来」。\n"
    )
    return f"""一、报告结构（严格按此顺序，不得调换）
1. **## 核心结论**（必须是第一个二级章节）
   - 列出整份报告中最重要的 5-8 条发现，每条一行，格式：「**结论标题**：具体说明（含数字）」
   - 覆盖所有 Part 的关键洞察，让读者读完此节即可掌握全部重点
{satisfaction_rule}
{context_rule}   - 只把 `<stats>` 中存在的数字写成事实；只把 `<open_text_themes>` 或 `<open_text_fallback>` 中存在的玩家反馈写成玩家观点；推测必须明确标注。
2. **## Part 1 受访者画像**（固定为第一个 Part，紧接核心结论之后）
   - 画像分布数据用 Markdown 表格呈现（列：维度 / 选项 / 人数占比），不要纯文字罗列
   - 表格之后用 1-2 句话解读画像特征
3. 其余 Part 按方案顺序逐章展开
4. **## 行动建议**（最后一节，3-5 条，每条必须有对应数据依据）

二、结论驱动
- 以"多少人持有什么观点"为核心叙事框架
- 每个结论必须附具体数字（人数或占比），禁止使用"部分用户""少数玩家"等模糊表述

三、主观题原文展示（关键）
- 每个主题/观点至少引用 3 条代表性玩家原文
- 展示格式：先展示原始语言原文（用引号括起），下方紧跟中文翻译（若原文已是中文则免翻译）
  示例：
  > "She's very outdated compared to other mage heroes."（该英雄与其他法师相比显得十分过时。）
  > "Modelnya kurang dipoles."（模型精致度不足。）
  > "模型感觉太老了，需要 revamp。"
- 引用的原文要能支撑该主题的核心论断，优先选择信息量最丰富的
- 若某主题可用原文不足 3 条，则展示全部可用原文，不要编造或重复引用

四、语言风格
- 简洁直接，去掉冗长铺垫和过渡句
- 报告语言为中文；玩家原文保留原语种并附中文翻译"""


def _writer_parts_meta(plan: dict, headers: list[str]) -> list[dict]:
    """返回 [{'i','name','col_desc'}]，供分轮生成时逐 Part 取标题与列说明。"""
    meta = []
    for i, p in enumerate(plan["parts"], 1):
        col_names = []
        for idx in p["column_indexes"]:
            col = next((c for c in plan["columns"] if c["index"] == idx), None)
            name = (col and col.get("name")) or (headers[idx] if idx < len(headers) else f"列{idx}")
            role = col["role"] if col else "?"
            col_names.append(f"{name}({role})")
        meta.append({"i": i, "name": p["name"], "col_desc": "; ".join(col_names)})
    return meta


def _build_writer_context(stats_md: str, open_text: dict, plan: dict, headers: list[str]) -> tuple[str, str, str]:
    """构造 Writer 的完整上下文：(plan_summary, open_text_md, requirements)。
    plan_summary/open_text/stats 仅在多轮生成的第 1 轮发送一次，后续轮次复用会话历史。"""
    parts_meta = _writer_parts_meta(plan, headers)
    parts_lines = [f"  Part {m['i']} {m['name']}: {m['col_desc']}" for m in parts_meta]
    plan_summary = "<plan>\n报告结构：\n" + "\n".join(parts_lines) + "\n</plan>"
    branch_logic_block = _build_branch_logic_block(plan.get("branch_rules"))
    if branch_logic_block:
        plan_summary += "\n\n" + branch_logic_block

    open_text_blocks = []
    for col_idx, texts in open_text.items():
        col = next((c for c in plan["columns"] if c["index"] == col_idx), None)
        name = (col and col.get("name")) or (headers[col_idx] if col_idx < len(headers) else f"列{col_idx}")
        name = f"{name}{_open_text_source_note(texts)}"
        name = _question_name_with_branch(name, plan, col_idx)
        joined_lines = []
        for entry in texts:
            ids = entry.get("ids", {})
            player_vals = [str(v).strip() for v in ids.values() if str(v).strip()]
            player_id = f"玩家ID={' / '.join(player_vals)}" if player_vals else ""
            profile_str = " / ".join(f"{k}={v}" for k, v in entry.get("profile", {}).items())
            prefix = " | ".join(filter(None, [player_id, f"画像={profile_str}" if profile_str else ""]))
            text_val = entry.get("text", "")
            joined_lines.append(f"- {f'[{prefix}] ' if prefix else ''}{text_val}")
        joined = "\n".join(joined_lines)
        open_text_blocks.append(f"### {name}（列 {col_idx}, 共 {len(texts)} 条非空回答）\n{joined}")

    open_text_md = (
        "<open_text>\n" + "\n\n".join(open_text_blocks) + "\n</open_text>"
        if open_text_blocks else "<open_text>（本问卷没有开放题）</open_text>"
    )

    requirements = _get_writer_requirements()
    if branch_logic_block:
        requirements += (
            "\n\n跳转题强制规则：必须执行 `<question_branch_logic>`；同一章节内的不同分支必须分别归纳，"
            "不得合并回答池。每条分支结论都要说明适用人群，并使用进入该分支人数或该题有效回答数作为分母，"
            "不得使用问卷总样本替代。不同题干、不同使用程度人群的主观反馈不得直接比较高低。"
        )
    requirements += (
        "\n\n补充：展示代表性玩家反馈时必须沿用 `<open_text>` 前缀里的玩家身份信息。"
        "所有 Discord、WhatsApp、MLBBID 或其它来源的身份值都已统一放在 `玩家ID=...` 中，"
        "报告表格只能使用一个 `玩家ID` 列，不得按来源拆列或改写表头。"
        "只有 `<open_text>` 前缀里真的存在 `画像=...` 时才可填写画像；没有画像时使用 `—`，不得编造。"
        "反馈表只能展示中文内容：中文回答原样展示，非中文回答翻译为中文，不得展示原始语言文本。"
    )
    return plan_summary, open_text_md, requirements


def _build_writer_first_query(
    stats_md: str,
    open_text: dict,
    plan: dict,
    headers: list[str],
    qualitative_context: dict | None = None,
) -> str:
    """多轮生成第 1 轮：发送全部上下文 + 要求，但本轮只让模型输出一级标题。"""
    plan_summary, open_text_md, requirements = _build_writer_context(stats_md, open_text, plan, headers)
    return (
        "**协作方式**：本次报告将**分多轮**生成。下面先给你全部数据（<plan> 报告结构、<stats> 确定性统计、"
        "<open_text> 全部开放题原文）和完整的写作要求。请通读并牢记——后续每一轮我会指定你写其中**某一个章节**，"
        "你要从这些数据里取材，但**每轮只写我当轮指定的部分，绝不提前写其它章节**。\n\n"
        f"{plan_summary}\n\n"
        f"<stats>\n{stats_md}\n</stats>\n\n"
        f"{open_text_md}\n\n"
        f"<report_spec>\n以下是整篇报告最终要满足的写作要求（供你理解全局，后续逐轮执行）：\n{requirements}\n</report_spec>"
        f"{_build_business_context_block(qualitative_context, '仅本轮注入，后续 part/bug/core 轮次请依赖本会话历史，不会重复提供')}\n\n"
        "**本轮任务（第 1 轮）**：**只**输出报告的一级标题（`# 一级标题`）。"
        "如果 <stats> 或 metadata 中存在「被排除」样本依据，可在标题下另起一行用一句话说明依据。"
        "除此之外**什么都不要写**——不要写核心结论、不要写任何 Part、不要写 Bug 模块、不要写本节总结。"
        "确认你已读完全部数据，本轮输出仅一级标题。"
    )


def _build_writer_part_query(part: dict) -> str:
    """多轮生成中的某个 Part 轮：仅指示写这一个 Part。原文已在会话历史中。"""
    return (
        f"**本轮任务**：现在**只**写 `## Part {part['i']} {part['name']}` 这一个章节的完整内容"
        f"（涉及列：{part['col_desc']}）。\n"
        "严格按 <report_spec> 里对 Part 的写法：紧接 `## Part` 标题后先写一段详尽的「本节总结」段落（连贯文字、不用列表），"
        "再围绕本 Part 的业务 Topic 综合展开。同一 Topic 下的客观题与相关开放题必须结合分析，客观统计作为人群背景和判断依据，"
        "主观反馈用于完整解释原因、情境、分歧与产品含义；不要按问卷题目逐题复述，也不要按正面/负面/中立机械拆分。"
        "本章内部禁止使用任何 `###` 或 `####` 标题，内部分析维度和观点名称一律使用加粗正文。"
        "每个观点使用 `**观点：短标题**`、完整观点说明、`提及情况：`、`**代表性玩家反馈：**` 的固定结构，"
        "并附 1–5 条 `玩家ID | 画像信息 | 中文翻译` 表格证据；只展示中文翻译，不保留原始语言文本。\n"
        "**约束**：① 只输出这一个 Part，不要写其它 Part；② 不要写核心结论、不要写 Bug 模块；"
        "③ 不要重复前面已经写过的标题或章节；④ 所有数字、百分比必须逐字取自 <stats>，禁止重算或编造。"
    )


def _build_writer_bug_query() -> str:
    """多轮生成的 Bug 模块轮：需要则只输出该模块，否则只回 NONE。"""
    return (
        "**本轮任务**：现在通览 <open_text> 里的**全部**开放反馈，按 <report_spec> 第 8 条判断是否需要 "
        "`## Bug 或待确认问题` 模块（仅当确有疑似功能 bug、体验异常、规则不明、玩家无法判断是否设计如此的问题时才需要）。\n"
        "- 若需要：**只**输出该模块——以 `## Bug 或待确认问题` 开头，下接 Markdown 表格，字段固定为 "
        "`问题类型`、`待确认问题`、`玩家信息`、`玩家原文翻译`，不要输出任何其它章节或解释。\n"
        "- 若不需要：**只**回复一个词 `NONE`，不要输出任何其它内容、不要解释。"
    )


def _build_writer_core_query(
    parts_meta: list[dict],
    has_bug: bool,
    qualitative_context: dict | None = None,
) -> str:
    """多轮生成的核心结论轮：基于已生成全部章节回写核心结论模块（放在报告顶部）。"""
    part_titles = "、".join(f"Part {m['i']} {m['name']}" for m in parts_meta)
    has_context = _has_business_context(qualitative_context)
    bug_clause = (
        "正文包含 `## Bug 或待确认问题` 模块，因此核心结论**最后必须**追加 `### 待确认问题概述`，只概述问题类型、不展开原文。"
        if has_bug else
        "正文没有 Bug 模块，因此核心结论**不要**写任何「待确认问题」相关小节。"
    )
    mode_clause = (
        "用户已提供 `<business_context>`：本模块是「业务判断层」，必须优先围绕用户填写的核心问题、目标用户、最关心问题和报告用途组织，"
        "直接回答这次调研要支持的业务判断；同时上提会影响决策的相关 topic（例如玩家痛点、明确不希望修改的部分、少数但高风险反馈、样本限制）。"
        "不要按 Part 机械复述，也不要只做资料摘要。\n"
        if has_context else
        "用户未提供 `<business_context>`：本模块是「基础发现层」，不得编造业务目标或假装知道产品决策背景。"
        "只能根据问卷题目、<stats>、<open_text> 和已生成章节归纳主要发现；如果需要判断这份调研可能关注什么，必须写成「从问卷内容推测/看起来」，并说明推测依据。\n"
    )
    return (
        "**本轮任务**：基于你前面已经生成的全部章节，撰写整篇报告的「核心结论」模块。"
        "这个模块最终会被放到报告**最顶部**（一级标题之后、各 Part 之前），所以请独立、完整地写出来。\n"
        f"{mode_clause}"
        "严格按 <report_spec> 里『核心结论』部分的格式：用 `<!--CORE_START-->` 和 `<!--CORE_END-->` 两个标记"
        "**各自独占一行**包裹整段，内部依次写：`## 核心结论`（首行写明样本总数）、`### 总体判断`、"
        f"若用户提供了业务问题，可按业务问题组织小节；否则逐个写 `### Part X 章节名：关键发现`（必须引用真实 Part 名：{part_titles}）；"
        "按需写 `### 高信号少数观点与风险`。\n"
        f"{bug_clause}\n"
        "**约束**：① 只输出从 `<!--CORE_START-->` 到 `<!--CORE_END-->` 的内容，不要重复正文章节、不要再写一级标题、不要写行动建议；"
        "② 核心结论里不使用百分比、不使用精确人数，改用量级描述（样本总数可引用）；"
        "③ 引用的绝对数值必须与 <stats> 一致；"
        "④ 玩家观点必须来自 <open_text> 或已生成章节，不得编造；"
        "⑤ 业务判断可以基于证据推断，但必须写清楚依据，凡是推测或猜测必须显式标注。"
    )


def _build_writer_action_query(
    parts_meta: list[dict],
    has_bug: bool,
    qualitative_context: dict | None = None,
) -> str:
    """多轮生成的行动建议轮（最后一轮）：基于已生成全部章节给出可执行的产品建议。"""
    part_titles = "、".join(f"Part {m['i']} {m['name']}" for m in parts_meta)
    has_context = _has_business_context(qualitative_context)
    bug_clause = (
        "正文包含 `## Bug 或待确认问题` 模块，行动建议里不要重复该模块已列出的具体问题项，必要时可提及但不展开。"
        if has_bug else ""
    )
    context_clause = (
        "若用户提供了 `<business_context>`，建议必须优先服务其中的核心问题和报告用途；"
        if has_context else
        "用户未提供 `<business_context>`，建议只能基于本报告中已经出现的证据提出，不要假设产品团队的具体目标；"
    )
    return (
        "**本轮任务（最后一轮）**：基于你前面已经生成的全部章节（"
        f"{part_titles}），撰写 `## 行动建议` 模块，这是整篇报告的最后一节。\n"
        "要求：\n"
        "1. 只输出这一个模块，以 `## 行动建议` 开头，不要重复或重写其它章节。\n"
        "2. 给出 3-5 条建议，每条使用固定结构：`**短标题**`，下面分别写「产品动作：」「优先级：」（高/中/低）"
        "「验证方式：」（说明需要什么数据、用户调研或实验来验证该建议）「依据：」（引用 <stats> 或 <open_text> 中的具体证据）"
        "「不确定性/前提：」（说明该建议的假设或局限）。\n"
        f"3. {context_clause}每条建议必须能在 <stats> 或 <open_text> 中找到对应依据，不得凭空提出。\n"
        "4. 如果建议依赖推测、猜测或样本外假设，必须在「不确定性/前提」里明确写出，不能包装成事实。\n"
        f"{bug_clause}"
    )


def _format_rows_for_qa(rows: list[list], plan: dict) -> str:
    QA_MAX = 60000
    if not rows or len(rows) <= 1:
        return "（无数据）"
    headers = rows[0]
    body = rows[1:]
    total = len(body)
    col_names = [(h or "").strip() or f"col_{i}" for i, h in enumerate(headers)]

    def row_obj(row):
        return {col_names[i]: (row[i] if i < len(row) else "") for i in range(len(col_names))}

    dump = "\n".join(json.dumps(row_obj(r), ensure_ascii=False) for r in body)
    if len(dump) > QA_MAX:
        pidxs = [c["index"] for c in plan.get("columns", []) if c.get("role") == "profile_dim"]
        sampled = _stratified_sample(body, pidxs, 100)
        note = (
            f"# 原始数据共 {total} 行，超出上下文上限，已按画像维度分层抽样到 {len(sampled)} 行。\n\n"
        )
        dump = note + "\n".join(json.dumps(row_obj(r), ensure_ascii=False) for r in sampled)
    return dump


def _stratified_sample(body: list[list], profile_indexes: list[int], target: int = 100):
    if not profile_indexes or len(body) <= target:
        return body[:target]

    def key(row):
        return tuple(row[i] if i < len(row) else "" for i in profile_indexes)

    buckets: dict = {}
    for r in body:
        buckets.setdefault(key(r), []).append(r)

    out: list = []
    total = len(body)
    for items in buckets.values():
        share = max(1, round(len(items) / total * target))
        out.extend(items[:share])
        if len(out) >= target:
            break
    return out[:target]


def _analyst_key_for_report(obj: dict) -> tuple[str, str]:
    """Return the Dify key that owns this report conversation."""
    analyst_app = obj.get("analyst_app") or ""
    mode = obj.get("mode") or obj.get("plan", {}).get("mode", "")
    if analyst_app == "large" or mode == "crosstab":
        return DIFY_LARGE_ANALYST_KEY, "DIFY_LARGE_ANALYST_KEY"
    return DIFY_ANALYST_KEY, "DIFY_ANALYST_KEY"
