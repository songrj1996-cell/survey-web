"""services/report_engine:定性报告生成引擎。

planner/writer 问询构建、大样本分批定性分析、开放题兜底、统计上下文拼装、QA 取数。
"""
import asyncio
import json
import re
from contextlib import suppress

from app.core.config import (
    BATCH_SIZE,
    LLM_CLASSIFY_CONCURRENCY,
    LLM_CLASSIFY_FALLBACK_MODELS,
    LLM_CLASSIFY_MAX_TOKENS,
    LLM_CLASSIFY_MODEL,
    LLM_CLASSIFY_REASONING,
    LLM_STREAM_HEARTBEAT_SECONDS,
    LLM_THEME_EXTRACT_CONCURRENCY,
    LLM_THEME_EXTRACT_FALLBACK_MODELS,
    LLM_THEME_EXTRACT_MAX_TOKENS,
    LLM_THEME_EXTRACT_MODEL,
    LLM_THEME_EXTRACT_REASONING,
    LLM_THEME_MERGE_FALLBACK_MODELS,
    LLM_THEME_MERGE_MAX_TOKENS,
    LLM_THEME_MERGE_MODEL,
    LLM_THEME_MERGE_REASONING,
    OTHER_THEME_PCT,
)
from app.integrations.llm_client import collect_chat_completion
from app.services.branch_logic import branch_rule_for_column, branch_rule_label
from app.services.question_detect import ROLE_LABEL_MAP
from app.storage.prompts import (
    _get_large_sample_writer_requirements as _get_large_sample_writer_requirements_base,
    _get_planner_extra,
    _get_response_classify_system_prompt,
    _get_theme_extract_system_prompt,
    _get_theme_merge_system_prompt,
    _get_writer_requirements,
)

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
        if role in (
            "single_choice", "profile_dim", "multi_choice", "matrix_single", "matrix_multi",
        ) and q.get("options"):
            opts = "、".join(str(o) for o in q["options"][:12])
            extra += f"，选项: {opts}"
        if role in ("multi_choice",) and q.get("delimiter"):
            extra += f"，分隔符: 「{q['delimiter']}」"
        if role in ("scale", "matrix_scale") and q.get("scale_min") is not None:
            extra += f"，量程: {q.get('scale_min')}–{q.get('scale_max')}"

        if role in ("matrix_scale", "matrix_single", "matrix_multi"):
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
    profile_indexes = sorted({
        col["index"]
        for col in (plan or {}).get("columns", [])
        if col.get("role") == "profile_dim" and isinstance(col.get("index"), int)
    })
    if profile_indexes:
        profile_constraint = (
            "6. 交叉分析约束：cross_tabs 中每一项的 profile_index 必须是以下画像维度列号之一："
            f"{profile_indexes}；不得为 null，不能使用其他题目列号。\n"
        )
    else:
        profile_constraint = (
            "6. 交叉分析约束：当前方案没有画像维度列，cross_tabs 必须为 []；"
            "不得输出 profile_index 为 null 的交叉分析项。\n"
        )
    return (
        "你正在修订一份问卷分析方案。请根据用户的修改意见，在当前方案基础上输出一份完整的新 plan JSON。\n\n"
        "严格要求：\n"
        "1. 只能输出一个完整 JSON 对象，不要输出解释、确认语、Markdown 文本或 ```json 围栏外的内容。\n"
        "2. JSON 必须包含 columns、parts、cross_tabs、open_questions 字段，并通过既有 schema 校验。\n"
        "3. columns 必须保留用户已确认的题型、列号、选项、矩阵题分组等权威信息；不要重新猜测题型或选项。\n"
        "4. parts 必须使用实际存在的列号；矩阵题成员列必须整体归入同一个 part。\n"
        "5. 若用户意见只要求调整章节/分析重点，只改 parts、cross_tabs 或 open_questions，不要无故改 columns。\n"
        f"{profile_constraint}\n"
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
    qualitative_context: dict | None = None,
) -> str:
    """跑数表模式：给章节策划 LLM 的初始 query。"""
    q_text = (questionnaire_text or "").strip()
    if len(q_text) > 12000:
        q_text = q_text[:12000] + "\n…（问卷过长，已截断）"
    avail = "\n".join(f"- {q}" for q in available_questions) or "（无）"
    opens = "\n".join(f"- {q}" for q in open_question_names) or "（无）"
    return (
        f"<questionnaire>\n{q_text}\n</questionnaire>\n\n"
        f"<available_questions>\n{avail}\n</available_questions>\n\n"
        f"<open_questions_list>\n{opens}\n</open_questions_list>\n\n"
        f"{_build_business_context_block(qualitative_context, '用于确定定量报告的章节结构和统计解读重点')}\n\n"
        "请基于以上规划报告章节大纲，按 system prompt 约定的 JSON 格式输出。"
    )


def _build_crosstab_plan_revision_query(
    questionnaire_text: str,
    available_questions: list[str],
    open_question_names: list[str],
    current_parts: list[dict],
    user_text: str,
    qualitative_context: dict | None = None,
) -> str:
    """跑数表模式：章节大纲修订 query，显式补齐无会话直连所需上下文。"""
    q_text = (questionnaire_text or "").strip()
    if len(q_text) > 12000:
        q_text = q_text[:12000] + "\n…（问卷过长，已截断）"
    avail = "\n".join(f"- {q}" for q in available_questions) or "（无）"
    opens = "\n".join(f"- {q}" for q in open_question_names) or "（无）"
    outline = json.dumps(current_parts or [], ensure_ascii=False, indent=2)
    return (
        f"<questionnaire>\n{q_text}\n</questionnaire>\n\n"
        f"<available_questions>\n{avail}\n</available_questions>\n\n"
        f"<open_questions_list>\n{opens}\n</open_questions_list>\n\n"
        f"<current_outline>\n{outline}\n</current_outline>\n\n"
        f"<user_request>\n{user_text.strip()}\n</user_request>\n\n"
        f"{_build_business_context_block(qualitative_context, '用于辅助调整定量报告的章节和分析重点')}\n\n"
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


def _llm_json_messages(system_prompt: str, query: str) -> list[dict]:
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query},
    ]


async def _direct_json_call(
    system_prompt: str,
    query: str,
    *,
    models: tuple[str, ...],
    max_tokens: int,
    reasoning_effort: str | None,
    validator,
) -> dict:
    """调用直连 LLM 并做一次针对 JSON/业务 schema 的纠错重试。"""
    try:
        answer, model = await collect_chat_completion(
            _llm_json_messages(system_prompt, query),
            models=models,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
    except Exception as exc:
        return {
            "data": None,
            "model": "",
            "raw_len": 0,
            "repaired": False,
            "error": str(exc)[:300],
        }

    parsed, parse_error = _json_loads_loose(answer)
    validation_error = validator(parsed) if parsed else (parse_error or "invalid JSON")
    if not validation_error:
        return {
            "data": parsed,
            "model": model,
            "raw_len": len(answer),
            "repaired": False,
            "error": "",
        }

    repair_messages = [
        *_llm_json_messages(system_prompt, query),
        {"role": "assistant", "content": answer[:24000]},
        {
            "role": "user",
            "content": (
                f"上一次输出未通过校验：{validation_error}。"
                "请修复后重新输出完整 JSON；不得解释、不得使用 Markdown 围栏。"
            ),
        },
    ]
    repair_models = (model, *(m for m in models if m != model))
    try:
        repaired_answer, repaired_model = await collect_chat_completion(
            repair_messages,
            models=repair_models,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
    except Exception as exc:
        return {
            "data": None,
            "model": model,
            "raw_len": len(answer),
            "repaired": True,
            "error": str(exc)[:300],
        }

    repaired, repair_parse_error = _json_loads_loose(repaired_answer)
    repair_validation_error = (
        validator(repaired) if repaired else (repair_parse_error or "invalid JSON")
    )
    return {
        "data": repaired if not repair_validation_error else None,
        "model": repaired_model,
        "raw_len": len(repaired_answer),
        "repaired": True,
        "error": repair_validation_error or "",
    }


async def _run_bounded_calls(call_factories: list, concurrency: int):
    """有限并发执行批次，并在等待期间产生 heartbeat 事件。"""
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _invoke(index: int, factory):
        async with semaphore:
            return index, await factory()

    pending = {
        asyncio.create_task(_invoke(index, factory))
        for index, factory in enumerate(call_factories)
    }
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending,
                timeout=LLM_STREAM_HEARTBEAT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                yield ("heartbeat", None)
                continue
            for task in done:
                yield ("result", task.result())
    finally:
        for task in pending:
            task.cancel()
        for task in pending:
            with suppress(asyncio.CancelledError):
                await task


def _text_key(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _validate_theme_candidates(data: dict | None, source_texts: list[str]) -> str | None:
    if not isinstance(data, dict):
        return "JSON 根节点必须是对象"
    themes = data.get("themes")
    if not isinstance(themes, list) or not themes:
        return "themes 必须是非空数组"
    if len(themes) > 15:
        return f"themes 数量为 {len(themes)}，不得超过 15"
    source_set = {_text_key(text) for text in source_texts if _text_key(text)}
    seen_names: set[str] = set()
    for index, theme in enumerate(themes):
        if not isinstance(theme, dict):
            return f"themes[{index}] 不是对象"
        name = str(theme.get("name") or "").strip()
        if not name:
            return f"themes[{index}] 缺少 name"
        name_key = name.casefold()
        if name_key in seen_names:
            return f"主题名称重复：{name}"
        seen_names.add(name_key)
        if not str(theme.get("description") or "").strip():
            return f"主题「{name}」缺少 description"
        for field in ("positive_summary", "negative_summary"):
            value = theme.get(field)
            if value is not None and not isinstance(value, str):
                return f"主题「{name}」的 {field} 必须是字符串或 null"
        quotes = theme.get("representative_quotes")
        if not isinstance(quotes, list) or not quotes or len(quotes) > 3:
            return f"主题「{name}」必须包含 1–3 条 representative_quotes"
        for quote in quotes:
            if _text_key(quote) not in source_set:
                return f"主题「{name}」包含并非逐字来自输入的引用"
    return None


def _validate_merged_themes(data: dict | None, candidates: list[dict]) -> str | None:
    if not isinstance(data, dict):
        return "JSON 根节点必须是对象"
    themes = data.get("themes")
    if not isinstance(themes, list) or not themes:
        return "themes 必须是非空数组"
    unique_candidates = {
        str(theme.get("name") or "").strip().casefold()
        for theme in candidates
        if str(theme.get("name") or "").strip()
    }
    required_min = min(10, len(unique_candidates))
    if len(themes) < required_min or len(themes) > 25:
        return (
            f"最终主题数量为 {len(themes)}，本次必须在 "
            f"{required_min}–25 个之间；不要用宽泛上位主题过度合并"
        )
    expected_ids = [f"t{i:02d}" for i in range(1, len(themes) + 1)]
    actual_ids = [theme.get("id") if isinstance(theme, dict) else None for theme in themes]
    if actual_ids != expected_ids:
        return f"主题 ID 必须从 t01 连续编号，期望 {expected_ids}"
    quote_pool = {
        _text_key(quote)
        for theme in candidates
        if isinstance(theme, dict)
        for quote in (theme.get("representative_quotes") or [])
        if _text_key(quote)
    }
    seen_names: set[str] = set()
    for theme in themes:
        name = str(theme.get("name") or "").strip()
        if not name or not str(theme.get("description") or "").strip():
            return f"主题 {theme.get('id')} 缺少 name 或 description"
        key = name.casefold()
        if key in seen_names:
            return f"最终主题名称重复：{name}"
        seen_names.add(key)
        for field in ("positive_summary", "negative_summary"):
            value = theme.get(field)
            if value is not None and not isinstance(value, str):
                return f"主题「{name}」的 {field} 必须是字符串或 null"
        quotes = theme.get("representative_quotes")
        if not isinstance(quotes, list) or not quotes or len(quotes) > 3:
            return f"主题「{name}」必须包含 1–3 条 representative_quotes"
        for quote in quotes:
            if _text_key(quote) not in quote_pool:
                return f"主题「{name}」包含候选主题中不存在的引用"
    return None


_VALID_SENTIMENTS = {"positive", "negative", "neutral", "mixed"}


def _normalize_classifications(
    data: dict | None,
    expected_ids: list[str],
    valid_theme_ids: set[str],
) -> dict[str, list[dict]]:
    """只保留完全合法且不重复的分类；其余 ID 交给 miss 修复。"""
    if not isinstance(data, dict) or not isinstance(data.get("classifications"), list):
        return {}
    expected = set(expected_ids)
    normalized: dict[str, list[dict]] = {}
    duplicate_ids: set[str] = set()
    for item in data["classifications"]:
        if not isinstance(item, dict):
            continue
        response_id = str(item.get("response_id") or "")
        if response_id not in expected:
            continue
        if response_id in normalized:
            duplicate_ids.add(response_id)
            continue
        assignments = item.get("assignments")
        if not isinstance(assignments, list) or not 1 <= len(assignments) <= 3:
            continue
        clean_assignments: list[dict] = []
        seen_themes: set[str] = set()
        valid = True
        for assignment in assignments:
            if not isinstance(assignment, dict):
                valid = False
                break
            theme_id = str(assignment.get("theme_id") or "")
            sentiment = str(assignment.get("sentiment") or "")
            if (
                theme_id not in valid_theme_ids
                or sentiment not in _VALID_SENTIMENTS
                or theme_id in seen_themes
            ):
                valid = False
                break
            seen_themes.add(theme_id)
            clean_assignments.append(
                {"theme_id": theme_id, "sentiment": sentiment}
            )
        if not valid:
            continue
        if "other" in seen_themes and (
            len(clean_assignments) != 1
            or clean_assignments[0]["sentiment"] != "neutral"
        ):
            continue
        normalized[response_id] = clean_assignments
    for response_id in duplicate_ids:
        normalized.pop(response_id, None)
    return normalized


def _build_theme_extract_query(
    question: str,
    batch: list[dict],
) -> tuple[str, list[str]]:
    texts = [str(entry.get("text") or "") for entry in batch]
    responses = "\n".join(f"[{index}] {text}" for index, text in enumerate(texts))
    return (
        f"<question>\n{question}\n</question>\n\n"
        f"<response_count>{len(batch)}</response_count>\n"
        f"<responses>\n{responses}\n</responses>",
        texts,
    )


def _build_theme_merge_query(
    question: str,
    candidates: list[dict],
    total_responses: int,
) -> str:
    return (
        f"<question>\n{question}\n</question>\n"
        f"<total_responses>{total_responses}</total_responses>\n"
        "<theme_candidates_json>\n"
        f"{json.dumps(candidates, ensure_ascii=False)}\n"
        "</theme_candidates_json>"
    )


def _build_classify_query(
    question: str,
    final_themes: list[dict],
    batch: list[dict],
    response_ids: list[str] | None = None,
) -> str:
    ids = response_ids or [str(index) for index in range(len(batch))]
    responses = "\n".join(
        f"[{response_id}] {batch[int(response_id)].get('text', '')}"
        for response_id in ids
    )
    theme_list = [
        {
            "id": theme["id"],
            "name": theme["name"],
            "description": theme["description"],
        }
        for theme in final_themes
    ]
    return (
        f"<question>\n{question}\n</question>\n"
        f"<themes_json>\n{json.dumps(theme_list, ensure_ascii=False)}\n</themes_json>\n"
        f"<responses>\n{responses}\n</responses>"
    )


async def _classify_batch_direct(
    question: str,
    final_themes: list[dict],
    batch: list[dict],
) -> dict:
    expected_ids = [str(index) for index in range(len(batch))]
    valid_theme_ids = {theme["id"] for theme in final_themes} | {"other"}
    system_prompt = _get_response_classify_system_prompt()
    models = (LLM_CLASSIFY_MODEL, *LLM_CLASSIFY_FALLBACK_MODELS)

    def _root_validator(data):
        if not isinstance(data, dict) or not isinstance(
            data.get("classifications"), list
        ):
            return "classifications 必须是数组"
        return None

    first = await _direct_json_call(
        system_prompt,
        _build_classify_query(question, final_themes, batch),
        models=models,
        max_tokens=LLM_CLASSIFY_MAX_TOKENS,
        reasoning_effort=LLM_CLASSIFY_REASONING or None,
        validator=_root_validator,
    )
    normalized = _normalize_classifications(
        first.get("data"), expected_ids, valid_theme_ids
    )
    missing_ids = [response_id for response_id in expected_ids if response_id not in normalized]
    repaired_count = 0
    repair_model = ""

    if missing_ids:
        miss = await _direct_json_call(
            system_prompt,
            _build_classify_query(
                question,
                final_themes,
                batch,
                response_ids=missing_ids,
            ),
            models=models,
            max_tokens=LLM_CLASSIFY_MAX_TOKENS,
            reasoning_effort=LLM_CLASSIFY_REASONING or None,
            validator=_root_validator,
        )
        repaired = _normalize_classifications(
            miss.get("data"), missing_ids, valid_theme_ids
        )
        normalized.update(repaired)
        repaired_count = len(repaired)
        repair_model = miss.get("model", "")

    fallback_ids = [
        response_id for response_id in expected_ids if response_id not in normalized
    ]
    for response_id in fallback_ids:
        normalized[response_id] = [{"theme_id": "other", "sentiment": "neutral"}]

    return {
        "classifications": [
            {
                "response_id": response_id,
                "assignments": normalized[response_id],
            }
            for response_id in expected_ids
        ],
        "model": first.get("model", ""),
        "repair_model": repair_model,
        "raw_len": first.get("raw_len", 0),
        "repaired_count": repaired_count,
        "fallback_count": len(fallback_ids),
        "error": first.get("error", ""),
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

        extract_factories = []
        for bi, batch in enumerate(batches, 1):
            yield ("progress", f"【{col_name}】提取主题（批次 {bi}/{len(batches)}）")
            query, source_texts = _build_theme_extract_query(col_name, batch)

            async def _extract(
                query=query,
                source_texts=source_texts,
            ):
                return await _direct_json_call(
                    _get_theme_extract_system_prompt(),
                    query,
                    models=(
                        LLM_THEME_EXTRACT_MODEL,
                        *LLM_THEME_EXTRACT_FALLBACK_MODELS,
                    ),
                    max_tokens=LLM_THEME_EXTRACT_MAX_TOKENS,
                    reasoning_effort=LLM_THEME_EXTRACT_REASONING or None,
                    validator=lambda data: _validate_theme_candidates(
                        data, source_texts
                    ),
                )

            extract_factories.append(_extract)

        extracted_by_batch: dict[int, list[dict]] = {}
        async for event_type, payload in _run_bounded_calls(
            extract_factories,
            LLM_THEME_EXTRACT_CONCURRENCY,
        ):
            if event_type == "heartbeat":
                yield ("heartbeat", "")
                continue
            batch_index, result = payload
            bi = batch_index + 1
            parsed = result.get("data")
            themes = parsed.get("themes", []) if isinstance(parsed, dict) else []
            phase_a = {
                "batch": bi,
                "raw_len": result.get("raw_len", 0),
                "parsed": bool(themes),
                "themes": len(themes),
                "model": result.get("model", ""),
                "repaired": bool(result.get("repaired")),
                "error": result.get("error", ""),
            }
            diag["phase_a"].append(phase_a)
            if themes:
                extracted_by_batch[bi] = themes
            else:
                yield (
                    "progress",
                    f"【{col_name}】主题提取失败（批次 {bi}），继续处理后续批次",
                )

        diag["phase_a"].sort(key=lambda item: item["batch"])
        for bi in sorted(extracted_by_batch):
            all_candidates.extend(extracted_by_batch[bi])

        if not all_candidates:
            diag["status"] = "failed"
            diag["reason"] = diag["reason"] or "主题提取未返回 themes"
            yield ("progress", f"【{col_name}】没有提取到主题，后续报告将尝试使用原文兜底")
            continue

        # ── Phase B：合并去重 ──────────────────────────────────────────────
        yield ("progress", f"【{col_name}】合并主题候选（共 {len(all_candidates)} 个）")
        merge_result = None

        async def _merge():
            return await _direct_json_call(
                _get_theme_merge_system_prompt(),
                _build_theme_merge_query(col_name, all_candidates, total),
                models=(
                    LLM_THEME_MERGE_MODEL,
                    *LLM_THEME_MERGE_FALLBACK_MODELS,
                ),
                max_tokens=LLM_THEME_MERGE_MAX_TOKENS,
                reasoning_effort=LLM_THEME_MERGE_REASONING or None,
                validator=lambda data: _validate_merged_themes(
                    data, all_candidates
                ),
            )

        async for event_type, payload in _run_bounded_calls([_merge], 1):
            if event_type == "heartbeat":
                yield ("heartbeat", "")
            else:
                _batch_index, merge_result = payload

        merge_result = merge_result or {}
        merged = merge_result.get("data")
        final_themes = merged.get("themes", []) if isinstance(merged, dict) else []
        diag["phase_b"] = {
            "raw_len": merge_result.get("raw_len", 0),
            "parsed": bool(final_themes),
            "themes": len(final_themes),
            "model": merge_result.get("model", ""),
            "repaired": bool(merge_result.get("repaired")),
            "error": merge_result.get("error", ""),
        }
        diag["themes"] = len(final_themes)

        if not final_themes:
            diag["status"] = "failed"
            diag["reason"] = (
                f"主题合并失败：{diag['phase_b']['error'] or '未返回 themes'}"
            )
            yield ("progress", f"【{col_name}】主题合并为空，后续报告将尝试使用原文兜底")
            continue

        # ── Phase C：回跑分类 ──────────────────────────────────────────────
        # counts[theme_id] = {"total": int, "pos": int, "neg": int, "neutral": int, "mixed": int}
        counts: dict[str, dict] = {t["id"]: {"total": 0, "pos": 0, "neg": 0, "neutral": 0, "mixed": 0}
                                    for t in final_themes}
        counts["other"] = {"total": 0, "pos": 0, "neg": 0, "neutral": 0, "mixed": 0}
        # quotes_pool[theme_id] = list of (sentiment, text)
        quotes_pool: dict[str, list] = {t["id"]: [] for t in final_themes}

        classify_factories = []
        for bi, batch in enumerate(batches, 1):
            yield ("progress", f"【{col_name}】分类回复（批次 {bi}/{len(batches)}）")

            async def _classify(batch=batch):
                return await _classify_batch_direct(col_name, final_themes, batch)

            classify_factories.append(_classify)

        classified_by_batch: dict[int, dict] = {}
        async for event_type, payload in _run_bounded_calls(
            classify_factories,
            LLM_CLASSIFY_CONCURRENCY,
        ):
            if event_type == "heartbeat":
                yield ("heartbeat", "")
                continue
            batch_index, result = payload
            classified_by_batch[batch_index] = result

        for batch_index in sorted(classified_by_batch):
            bi = batch_index + 1
            batch = batches[batch_index]
            result = classified_by_batch[batch_index]
            classifications = result.get("classifications", [])
            phase_c = {
                "batch": bi,
                "raw_len": result.get("raw_len", 0),
                "parsed": bool(classifications),
                "classifications": len(classifications),
                "assignments": 0,
                "model": result.get("model", ""),
                "repair_model": result.get("repair_model", ""),
                "missing_repaired": result.get("repaired_count", 0),
                "missing_fallback": result.get("fallback_count", 0),
                "error": result.get("error", ""),
            }
            diag["classifications"] += len(classifications)

            for item in classifications:
                resp_idx = int(item["response_id"])
                original_text = (
                    batch[resp_idx].get("text", "")
                    if 0 <= resp_idx < len(batch)
                    else ""
                )
                assignments = item["assignments"]
                phase_c["assignments"] += len(assignments)
                for assign in assignments:
                    tid = assign["theme_id"]
                    sentiment = assign["sentiment"]
                    counts[tid]["total"] += 1
                    if sentiment == "positive":
                        counts[tid]["pos"] += 1
                    elif sentiment == "negative":
                        counts[tid]["neg"] += 1
                    elif sentiment == "mixed":
                        counts[tid]["mixed"] += 1
                    else:
                        counts[tid]["neutral"] += 1
                    if (
                        tid != "other"
                        and len(quotes_pool[tid]) < 10
                        and original_text
                    ):
                        quotes_pool[tid].append((sentiment, original_text))
            diag["assignments"] += phase_c["assignments"]
            diag["phase_c"].append(phase_c)

        # ── 统计汇总 ──────────────────────────────────────────────────────
        classified_responses = sum(
            item.get("classifications", 0) for item in diag["phase_c"]
        )
        if classified_responses == 0 or total == 0:
            diag["status"] = "failed"
            diag["reason"] = "分类阶段未产生任何主题归属"
            yield ("progress", f"【{col_name}】分类未产生有效归属，后续报告将尝试使用原文兜底")
            continue
        diag["percentage_basis"] = "response_coverage"
        diag["percentage_denominator"] = total

        themes_out = []
        other_themes_out = []

        for t in final_themes:
            tid = t["id"]
            c = counts[tid]
            cnt = c["total"]
            pct = round(cnt / total * 100, 1)
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
    quantitative_first: bool = False,
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
        lines.append(
            "主题占比口径：提到该主题的回答数 ÷ 本题有效回答总数；"
            "一条回答可属于多个主题，因此各主题占比之和可能超过 100%。\n"
        )

        for i, t in enumerate(data["themes"], 1):
            lines.append(
                f"**主题{i}：{t['name']}**"
                f"（{t['count']:,} 条回答提及，占 {t['percentage']}%）"
            )
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
    qualitative_stats_rule = (
        "\n- 系统会在对应 Part 内确定性插入客观题统计表。不要自行复制客观题统计表或新增统计章节；"
        "必须在本节总结或后续主题分析中说明相关统计大致代表什么、有哪些样本或解释限制，"
        "无法形成可靠解释时不要机械复述最高项和最低项。"
        if not quantitative_first else ""
    )

    return (
        "**任务**：基于以下大样本问卷分析结果撰写调研报告。\n\n"
        + f"{plan_summary}\n\n"
        + (f"{branch_logic_block}\n\n" if branch_logic_block else "")
        + f"<stats>\n{stats_md}\n</stats>\n\n"
        + f"{priority_block}"
        + f"{open_text_md}\n\n"
        + (f"{fallback_md}\n\n" if fallback_md else "")
        + f"**要求**：\n{requirements}{qualitative_stats_rule}"
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
        "- **满意度优先原则**：`<priority_metrics>` 中已提取满意度数据，必须将其作为核心结论中最靠前的 1-2 条展示，须包含具体数字"
        if has_satisfaction else
        "- **满意度优先原则**：若报告中存在任何与满意度评分/评价相关的数据（如好评率、满意度评分、认可度等），必须将其作为核心结论中最靠前的 1-2 条展示，且须包含具体数字"
    )
    context_rule = (
        "- 用户已提供 `<business_context>`：核心结论必须优先回答其中的核心问题，并纳入会影响决策的相关 topic、风险和样本限制；不要按 Part 机械复述。"
        if has_business_context else
        "- 用户未提供 `<business_context>`：不得编造业务目标；只能根据问卷题目、统计结果和玩家反馈归纳基础发现。如需判断调研意图，必须写成「从问卷内容推测/看起来」。"
    )
    base = _get_large_sample_writer_requirements_base().rstrip()
    return (
        f"{base}\n\n"
        "五、当前数据条件规则\n"
        f"{satisfaction_rule}\n"
        f"{context_rule}"
    )


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
        "\n\n补充：在「相关具体信息引用」中展示玩家反馈时，必须沿用 `<open_text>` 前缀里的玩家身份信息。"
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


def _build_writer_part_query(
    part: dict,
    *,
    quantitative_first: bool = False,
) -> str:
    """多轮生成中的某个 Part 轮：仅指示写这一个 Part。原文已在会话历史中。"""
    quantitative_rule = (
        "本次为定量优先报告：本节先说明所有相关客观题的主要分布、最高/最低项和显著差异，"
        "再用开放题解释原因。至少引用一组最关键的客观统计作为判断依据；"
        "完整逐题统计表由系统在附录确定性插入，无需在正文机械复制全部表格。\n"
        if quantitative_first else ""
    )
    qualitative_rule = (
        "本次为定性报告：系统会在本节总结之后确定性插入客观题统计表。"
        "不要自行复制客观题统计表，也不要新增统计标题；须在本节总结或后续主题分析中说明相关统计"
        "大致代表什么、有哪些样本或解释限制，并结合开放题解释原因、情境与产品含义。"
        "无法形成可靠解释时不要机械复述最高项和最低项。\n"
        if not quantitative_first else ""
    )
    return (
        f"**本轮任务**：现在**只**写 `## Part {part['i']} {part['name']}` 这一个章节的完整内容"
        f"（涉及列：{part['col_desc']}）。\n"
        + quantitative_rule
        + qualitative_rule
        + "严格按 <report_spec> 里对 Part 的写法：紧接 `## Part` 标题后写 `**本节总结：**`，并用 3–6 条带加粗短标题的 Markdown 编号列表归纳关键结论，"
        "不得把全部数字和中文说明塞进一个超长段落；"
        "再围绕本 Part 的业务 Topic 综合展开。同一 Topic 下的客观题与相关开放题必须结合分析，客观统计作为人群背景和判断依据，"
        "主观反馈用于完整解释原因、情境、分歧与产品含义；不要按问卷题目逐题复述，也不要按正面/负面/中立机械拆分。"
        "本章内部禁止使用任何 `###` 或 `####` 标题，内部分析维度和观点名称一律使用加粗正文。"
        "每个观点使用 `**观点：短标题**`，并将主要发现、原因与情境、分歧或例外、产品含义拆成 2–4 条简短项目，"
        "再写 `**提及情况：**`。统计数据优先直接写进对应内容；需要单独列数据表或玩家反馈表时，表格前统一使用"
        " `**相关具体信息引用：**`，不得使用「代表性玩家反馈」，也不得留下空标题。玩家反馈表附 1–5 条"
        " `玩家ID | 画像信息 | 中文翻译` 证据，只展示中文翻译，不保留原始语言文本。\n"
        "本节总结必须用不看后文原文也能理解的大白话，优先沿用玩家中文翻译中的具体说法。"
        "不要用「功能性增益」「价值感知」「分层机制」这类抽象概念代替玩家实际在意的功能、体验或场景；"
        "确需概括时，须在同一句用「也就是……」或等价表达解释具体所指，且不得补造原文中没有的例子。\n"
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
        "`### 总体判断` 中每个判断必须主动说明它针对的具体对象、功能、方案、场景、人群或研究范围，"
        "让未参与调研立项、未看过问卷提纲的读者也能独立理解。若涉及多个容易混淆的范围，须按真实业务语义"
        "分别回车成短段，不使用 1、2、3 编号，不得写成一个超长段落，也不得使用无明确指代的「该方案」"
        "「这一问题」「核心分歧」开门见山。`### 高信号少数观点与风险` 每条也必须在短标题或首句明确对应的"
        "对象、功能、方案、场景、人群或研究范围，不同范围不得混写；范围名称只能来自 plan、题目、<open_text>"
        "或已生成章节，不得机械套用标签或补造研究阶段。\n"
        "**约束**：① 只输出从 `<!--CORE_START-->` 到 `<!--CORE_END-->` 的内容，不要重复正文章节、不要再写一级标题、不要写行动建议；"
        "② 核心结论里不使用百分比、不使用精确人数，改用量级描述（样本总数可引用）；"
        "③ 引用的绝对数值必须与 <stats> 一致；"
        "④ 玩家观点必须来自 <open_text> 或已生成章节，不得编造；"
        "⑤ 业务判断可以基于证据推断，但必须写清楚依据，凡是推测或猜测必须显式标注；"
        "⑥ 使用不看玩家原文也能立即理解的大白话，优先沿用玩家中文翻译中的具体词语。"
        "不要只写「功能性增益」「价值感知」「分层机制」等抽象概括；确需使用时，必须在同一句用「也就是……」"
        "或等价表达说明玩家具体希望增加、取消或改变什么，解释和例子只能来自 <open_text> 或已生成章节。"
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
        "2. 给出 3-5 条建议，并只使用一张 Markdown 表格呈现。表头和顺序必须逐字为："
        "`建议内容 | 优先级 | 产品动作 | 验证方式 | 依据 | 不确定性/前提`。"
        "`建议内容` 使用加粗短标题加一句核心判断；`优先级` 只能写高/中/低；"
        "其余列分别说明具体动作、所需数据/用户调研/实验、<stats> 或 <open_text> 中的具体证据、假设或局限。\n"
        f"3. {context_clause}每条建议必须能在 <stats> 或 <open_text> 中找到对应依据，不得凭空提出。\n"
        "4. 如果建议依赖推测、猜测或样本外假设，必须在「不确定性/前提」里明确写出，不能包装成事实。\n"
        f"{bug_clause}"
    )


def _build_writer_action_repair_query() -> str:
    """行动建议内容已生成但标题不合规时，仅修复 Markdown 结构。"""
    return (
        "上一轮已经完成行动建议内容，但输出没有使用规定的 Markdown 结构。"
        "请只重新整理上一轮内容的格式，不要改变建议、依据、优先级、验证方式或任何分析结论，"
        "也不要新增内容。输出必须从独占一行的 `## 行动建议` 开始，后面将原有 3-5 条建议整理为一张表格，"
        "表头和顺序逐字使用 `建议内容 | 优先级 | 产品动作 | 验证方式 | 依据 | 不确定性/前提`；"
        "不要输出解释、前言、其它章节或代码围栏。"
    )


def _normalize_action_section(text: str) -> str:
    """把常见的行动建议标题变体规范为固定二级标题；不改正文内容。"""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    heading = re.search(
        r"(?mi)^[ \t]*(?:#{1,6}[ \t]+)?(?:\*\*)?行动建议(?:\*\*)?[^\r\n]*$",
        cleaned,
    )
    if not heading:
        return ""
    body = cleaned[heading.end():].lstrip("\r\n ")
    return "## 行动建议" + (f"\n\n{body}" if body else "")


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


def _describe_qa_context_scope(qa_context: str) -> str:
    """Return a user-facing summary of the raw-feedback coverage in QA context."""
    rows_match = re.search(r"<rows>\s*(.*?)\s*</rows>", qa_context or "", re.DOTALL)
    rows_block = rows_match.group(1).strip() if rows_match else ""
    base = "报告正文、分析方案、统计结果和业务背景"
    if not rows_block or rows_block == "（无数据）":
        return f"{base}；该记录未保留可用的原始玩家反馈。"

    sampled = re.search(
        r"# 原始数据共\s*(\d+)\s*行，超出上下文上限，已按画像维度分层抽样到\s*(\d+)\s*行。",
        rows_block,
    )
    if sampled:
        return (
            f"{base}及按画像维度分层抽样的 {sampled.group(2)} 条原始玩家反馈"
            f"（原始共 {sampled.group(1)} 条）。"
        )

    row_count = sum(1 for line in rows_block.splitlines() if line.strip())
    if row_count:
        return f"{base}及全部 {row_count} 条原始玩家反馈。"
    return f"{base}；该记录未保留可用的原始玩家反馈。"


def _build_qa_context(source: dict, report_md: str | None = None) -> str:
    """构造可持久化的追问上下文，同时覆盖问卷、确定性分析结果和最终报告。"""
    plan = source.get("plan") or {}
    rows_block = _format_rows_for_qa(source.get("rows") or [], plan)
    questionnaire_text = str(source.get("questionnaire_text") or "").strip()
    qualitative_context = source.get("qualitative_context") or {}
    report = str(report_md if report_md is not None else source.get("report_md") or "").strip()
    stats_md = str(source.get("stats_md") or "").strip()

    blocks = [
        "<qa_context>",
        "你正在回答用户对一份调研报告的追问。下面同时提供最终报告、问卷结构、"
        "确定性统计、业务背景和原始回答上下文。回答时必须理解用户是在询问这份报告中的内容与逻辑；"
        "如报告表述与确定性统计或原始回答冲突，以确定性统计和原始回答为准，并明确指出冲突。",
        f"<report>\n{report or '（报告正文缺失）'}\n</report>",
        "<analysis_plan>\n"
        + json.dumps(plan, ensure_ascii=False, indent=2)
        + "\n</analysis_plan>",
        f"<stats>\n{stats_md or '（无统计结果）'}\n</stats>",
        "<business_context>\n"
        + json.dumps(qualitative_context, ensure_ascii=False, indent=2)
        + "\n</business_context>",
    ]
    if questionnaire_text:
        blocks.append(f"<questionnaire>\n{questionnaire_text}\n</questionnaire>")
    blocks.append(f"<rows>\n{rows_block}\n</rows>")
    blocks.append("</qa_context>")
    return "\n\n".join(blocks)


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
