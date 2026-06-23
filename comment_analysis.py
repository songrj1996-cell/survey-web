"""帖子评论舆情分析 · 预处理与统计（纯函数，无 Dify / 网络依赖）。

负责：文件解析、规则清洗、分层抽样、批次切分、本地统计聚合。
Dify 并发调用与 SSE 编排在 server.py 中完成，本模块只提供可单元测试的纯逻辑。

清洗规则、抽样比例、批次大小等阈值集中在本文件顶部，方便后续调参。
"""
import csv
import io
import json
import random
import re
import unicodedata

import openpyxl

# ── 可调参数 ──────────────────────────────────────────────

# 评论内容列名匹配关键词（大小写不敏感，子串匹配）
COMMENT_COL_KEYS = ("message", "comment", "评论", "内容", "留言", "content", "text")

MIN_LEN = 15            # 规则①长度过短：strip 后长度 < 此值即丢弃
KB_MASH_RATIO = 0.6     # 规则②键盘乱打：单个高频字符占比 > 此值
KB_MASH_MINLEN = 20     # 规则②键盘乱打：仅对长度 > 此值的评论生效

SAMPLE_MAX_N = 5000     # 分层抽样目标上限；有效评论 <= 此值则全量
# 长度分层边界与抽取比例（短 15~30 / 中 31~80 / 长 >80）
STRATA = (
    ("short", 15, 30, 0.50),
    ("mid", 31, 80, 0.35),
    ("long", 81, None, 0.15),
)

BATCH_SIZE = 250        # 每批发给 Dify 的评论条数（5000 → 20 批）

OTHER_THEME_PCT = 3.0   # 占比低于此值（%）的主题合并入「其他」

# 规则⑤纯索要奖励：整条评论只在索要钻石/皮肤/英雄/奖励等，无其他意见。
# 用「整条锚定匹配」实现“整条只索要”——夹带其他意见的评论不会整体命中，因而保留。
_REWARD_FILLER = r"(?:please|pls|plz|sir|admin|hi|hello|hey|free|me|my|us|the|a|some|more|give|gift|send|want|need|give\s+me)"
_REWARD_TARGET_EN = r"(?:skin|skins|diamond|diamonds|hero|heroes|reward|rewards|gift|gifts|coin|coins)"
_REWARD_PATTERNS = [
    # 英文：give/send/want ... skin/diamond ...（整条只有索要相关词 + 标点）
    re.compile(
        rf"^(?:[\s,.!?@#~*\-]*{_REWARD_FILLER}[\s,.!?@#~*\-]*)*"
        rf"{_REWARD_TARGET_EN}"
        rf"(?:[\s,.!?@#~*\-]*(?:{_REWARD_FILLER}|{_REWARD_TARGET_EN})[\s,.!?@#~*\-]*)*$",
        re.IGNORECASE,
    ),
    # 中文：求/请/给我（免费）钻石/皮肤/英雄/奖励
    re.compile(
        r"^[\s，。！？、~*\-]*"
        r"(?:求|请|给我|送我|想要|要|跪求|跪求送|免费)+"
        r"(?:个|点|些)?"
        r"(?:免费)?"
        r"(?:钻石|皮肤|英雄|奖励|礼包|金币|点券)+"
        r"[\s，。！？、~*\-]*$"
    ),
]


# ── 宽松 JSON 解析 ────────────────────────────────────────

def loads_loose(raw: str):
    """容错解析 LLM 返回的 JSON（dict 或 list 均可）。返回 (obj, err)。

    server.py 现有的 _json_loads_loose 只接受 JSON 对象，而本功能的
    extract / merge / classify 节点输出的是 JSON 数组，需单独处理。
    依次尝试：原文 → markdown 代码块内容 → [..] 边界 → {..} 边界。
    """
    raw = (raw or "").strip()
    if not raw:
        return None, "empty"
    candidates = [raw]
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S | re.I)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    ls, le = raw.find("["), raw.rfind("]")
    if 0 <= ls < le:
        candidates.append(raw[ls:le + 1])
    bs, be = raw.find("{"), raw.rfind("}")
    if 0 <= bs < be:
        candidates.append(raw[bs:be + 1])

    last_err = ""
    for text in candidates:
        try:
            return json.loads(text), ""
        except Exception as e:  # noqa: BLE001 - 容错解析，任何异常都继续尝试
            last_err = str(e)
    return None, (last_err[:180] or "invalid json")


# ── 文件解析 ──────────────────────────────────────────────

def _parse_csv(content: bytes) -> list[list[str]]:
    """多编码兼容的 CSV 解析（utf-8-sig 优先以兼容 BOM）。"""
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb2312"):
        try:
            text = content.decode(enc)
            reader = csv.reader(io.StringIO(text))
            rows = [list(row) for row in reader]
            while rows and all(not str(c).strip() for c in rows[-1]):
                rows.pop()
            return rows
        except (UnicodeDecodeError, csv.Error):
            continue
    raise ValueError("无法解析 CSV 文件，请确认文件编码为 UTF-8 或 GBK")


def _parse_excel(content: bytes) -> list[list[str]]:
    """只读第一个 sheet，所有值转字符串。"""
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    rows: list[list[str]] = []
    for row in ws.iter_rows(values_only=True):
        rows.append([("" if c is None else str(c)) for c in row])
    wb.close()
    while rows and all(not c.strip() for c in rows[-1]):
        rows.pop()
    return rows


def _locate_comment_column(header: list[str]) -> int:
    """根据列名关键词定位评论列；匹配不到时回退到最“文本化”的列。

    返回列索引。回退策略：选择表头里第一个非空、且不像 ID/时间/数字列名的列。
    """
    for idx, name in enumerate(header):
        low = str(name).strip().lower()
        if any(key in low for key in COMMENT_COL_KEYS):
            return idx
    # 回退：第一个非空表头列
    for idx, name in enumerate(header):
        if str(name).strip():
            return idx
    return 0


def parse_comment_file(file_bytes: bytes, filename: str) -> tuple[list[str], str]:
    """解析上传的评论文件，返回 (评论文本列表, 命中的列名)。

    仅支持 .csv / .xlsx / .xls。自动定位评论列，提取文本并去首尾空白，
    跳过空单元格。不在此处做清洗（清洗交给 filter_comments）。
    """
    name_low = (filename or "").lower()
    if name_low.endswith(".csv"):
        rows = _parse_csv(file_bytes)
    elif name_low.endswith((".xlsx", ".xls")):
        rows = _parse_excel(file_bytes)
    else:
        raise ValueError("仅支持 CSV / Excel（.csv / .xlsx / .xls）文件")

    if not rows:
        raise ValueError("文件为空")
    if len(rows) <= 1:
        raise ValueError("文件只有表头没有数据行")

    header = rows[0]
    col_idx = _locate_comment_column(header)
    col_name = str(header[col_idx]).strip() if col_idx < len(header) else ""

    comments: list[str] = []
    for row in rows[1:]:
        if col_idx >= len(row):
            continue
        text = str(row[col_idx]).strip()
        if text:
            comments.append(text)
    return comments, col_name


# ── 规则清洗 ──────────────────────────────────────────────

def has_any_letter(text: str) -> bool:
    """是否包含任意语言的字母字符。

    依据 Unicode category 首字母为 'L'：
    - Lo 覆盖泰语/阿拉伯语/CJK 等非拉丁字母
    - Ll/Lu/Lt/Lm 覆盖拉丁、西里尔等大小写字母
    数字(N*)、标点(P*)、符号(S*)、空白(Z*) 均不计入，从而避免误删
    泰语、阿拉伯语、越南语、西里尔语等多语种评论。
    """
    return any(unicodedata.category(c).startswith("L") for c in text)


def _is_keyboard_mash(text: str) -> bool:
    """键盘乱打：长度 > KB_MASH_MINLEN 且单个高频字符占比 > KB_MASH_RATIO。"""
    stripped = text.strip()
    if len(stripped) <= KB_MASH_MINLEN:
        return False
    counts: dict[str, int] = {}
    for c in stripped:
        if c.isspace():
            continue
        counts[c] = counts.get(c, 0) + 1
    non_space_len = sum(counts.values())
    if non_space_len == 0:
        return False
    top = max(counts.values())
    return top / non_space_len > KB_MASH_RATIO


def _is_reward_only(text: str) -> bool:
    """整条评论只在索要奖励（钻石/皮肤/英雄等），无其他实质意见。"""
    stripped = text.strip()
    return any(p.match(stripped) for p in _REWARD_PATTERNS)


def filter_comments(comments: list[str]) -> tuple[list[str], dict[str, int]]:
    """规则清洗。任一规则命中即丢弃；返回 (保留列表, 各规则丢弃计数)。

    规则（按命中即丢弃）：
    - too_short：strip 后长度 < MIN_LEN
    - keyboard_mash：单字符高频占比超阈值（"aaaaa..."、"asdfgh..."）
    - no_letter：不含任意语言字母字符（"123123"、"??!!!"）
    - duplicate：大小写不敏感完全去重，保留第一条
    - reward_only：整条只索要奖励

    去重在最后做，保证"保留第一条"指向通过前几条规则的首条。
    """
    stats = {
        "too_short": 0,
        "keyboard_mash": 0,
        "no_letter": 0,
        "reward_only": 0,
        "duplicate": 0,
    }
    seen: set[str] = set()
    kept: list[str] = []
    for raw in comments:
        text = raw.strip()
        if len(text) < MIN_LEN:
            stats["too_short"] += 1
            continue
        if _is_keyboard_mash(text):
            stats["keyboard_mash"] += 1
            continue
        if not has_any_letter(text):
            stats["no_letter"] += 1
            continue
        if _is_reward_only(text):
            stats["reward_only"] += 1
            continue
        key = text.lower()
        if key in seen:
            stats["duplicate"] += 1
            continue
        seen.add(key)
        kept.append(text)
    return kept, stats


# ── 关键词过滤（路径 B：有帖子原文时的二次过滤）──────────────

def filter_by_keywords(
    comments: list[str],
    topic_keywords: list[str],
    exclude_keywords: list[str],
) -> tuple[list[str], int]:
    """用 filter 节点产出的关键词做保守过滤，返回 (保留列表, 丢弃数)。

    判定（保守，少误杀）：一条评论同时满足「命中某个 exclude_keyword」
    且「不含任何 topic_keyword」才丢弃；只要沾了相关词就保留。
    大小写不敏感的子串匹配；无排除词时原样返回。
    """
    excludes = [k.strip().lower() for k in (exclude_keywords or []) if k and k.strip()]
    if not excludes:
        return list(comments), 0
    topics = [k.strip().lower() for k in (topic_keywords or []) if k and k.strip()]

    kept: list[str] = []
    dropped = 0
    for c in comments:
        low = c.lower()
        hit_exclude = any(e in low for e in excludes)
        hit_topic = any(t in low for t in topics)
        if hit_exclude and not hit_topic:
            dropped += 1
            continue
        kept.append(c)
    return kept, dropped


# ── 分层随机抽样 ──────────────────────────────────────────

def _stratum_of(text: str) -> str:
    n = len(text)
    for name, lo, hi, _ratio in STRATA:
        if n >= lo and (hi is None or n <= hi):
            return name
    # 长度 < 最小边界的（理论上已被 MIN_LEN 过滤）归入 short
    return STRATA[0][0]


def stratified_sample(
    valid_comments: list[str],
    max_n: int = SAMPLE_MAX_N,
    seed: int | None = 42,
) -> list[str]:
    """按长度分层随机抽样至多 max_n 条。

    - 有效评论数 <= max_n：全量返回（不抽样，保持原顺序）。
    - 否则按 STRATA 比例分配名额，桶内随机抽取；某桶不足时把剩余名额
      按比例重分配给其他桶，尽量取满 max_n。
    """
    if len(valid_comments) <= max_n:
        return list(valid_comments)

    rng = random.Random(seed)
    buckets: dict[str, list[str]] = {name: [] for name, *_ in STRATA}
    for text in valid_comments:
        buckets[_stratum_of(text)].append(text)

    # 初始名额
    targets: dict[str, int] = {
        name: int(round(max_n * ratio)) for name, _lo, _hi, ratio in STRATA
    }
    # 限制在桶容量内，记录缺额
    picked: dict[str, int] = {}
    deficit = 0
    for name, *_ in STRATA:
        want = targets[name]
        have = len(buckets[name])
        take = min(want, have)
        picked[name] = take
        deficit += want - take

    # 把缺额分配给仍有余量的桶
    if deficit > 0:
        for name, *_ in STRATA:
            if deficit <= 0:
                break
            spare = len(buckets[name]) - picked[name]
            if spare > 0:
                add = min(spare, deficit)
                picked[name] += add
                deficit -= add

    result: list[str] = []
    for name, *_ in STRATA:
        pool = buckets[name]
        k = picked[name]
        if k >= len(pool):
            result.extend(pool)
        else:
            result.extend(rng.sample(pool, k))
    # 防御：四舍五入可能略超 max_n
    if len(result) > max_n:
        result = rng.sample(result, max_n)
    return result


def make_batches(comments: list[str], batch_size: int = BATCH_SIZE) -> list[list[str]]:
    """把评论切分为若干批，供并发调用 Dify。"""
    return [comments[i:i + batch_size] for i in range(0, len(comments), batch_size)]


# ── 本地统计聚合 ──────────────────────────────────────────

def format_quote(translation: str, original: str) -> str:
    """代表性引用格式化为「中文翻译」（原文: ...）。

    翻译缺失时退化为只显示原文，避免出现空书名号。
    """
    original = (original or "").strip()
    translation = (translation or "").strip()
    if translation and translation != original:
        return f"「{translation}」（原文: {original}）"
    return f"「{original}」"


def aggregate(
    final_themes: list[dict],
    classifications: list[dict],
    other_pct: float = OTHER_THEME_PCT,
) -> dict:
    """对分类结果做本地统计：占比、情感分布、代表引用、低频合并入「其他」。

    多标签：一条评论可命中多个主题（theme_ids），分别计入各主题。

    入参：
    - final_themes: 合并去重后的主题列表，每项至少含 id / name / description。
    - classifications: 每条评论一条记录（顺序与原评论一致），形如：
        {
          "theme_ids": [str, ...],  # 命中的主题列表；不在 final_themes 中的归入 other
          "sentiment": "positive" | "negative" | "neutral" | "mixed",  # 评论整体情感
          "original": str,          # 原文评论
          "translation": str,       # 中文翻译（可空）
          "is_quote_candidate": bool,
        }
      兼容旧单标签字段 "theme_id"（若无 theme_ids 则按单主题处理）。

    返回：
    {
      "total_classified": int,      # 评论总数
      "sentiment_overall": {"positive_pct", "neutral_pct", "negative_pct"},  # 按评论计，和≈100%
      "themes": [ {id,name,description,count,percentage,
                   positive_count/pct, negative_count/pct, neutral_count/pct,
                   quotes:[...]} ],   # 占比 = 提及该主题的评论数/总评论数，多标签下各主题之和可能 >100%
      "other_themes": [ {name,count,percentage} ],  # 占比 < other_pct
    }
    """
    theme_ids = {t["id"] for t in final_themes}
    counts: dict[str, dict] = {
        t["id"]: {"total": 0, "positive": 0, "negative": 0, "neutral": 0, "mixed": 0}
        for t in final_themes
    }
    counts["other"] = {"total": 0, "positive": 0, "negative": 0, "neutral": 0, "mixed": 0}
    # 每主题、每情感的候选引用池
    quotes_pool: dict[str, dict[str, list[tuple[str, str]]]] = {
        t["id"]: {"positive": [], "negative": [], "neutral": []} for t in final_themes
    }

    overall = {"positive": 0, "negative": 0, "neutral": 0, "mixed": 0}
    comment_count = 0

    for item in classifications:
        comment_count += 1
        sentiment = item.get("sentiment", "neutral")
        if sentiment not in ("positive", "negative", "neutral", "mixed"):
            sentiment = "neutral"
        overall[sentiment] += 1  # 整体情感按"每条评论"只计一次

        # 多标签：取 theme_ids 列表；兼容旧单标签 theme_id
        tids = item.get("theme_ids")
        if not isinstance(tids, list) or not tids:
            single = item.get("theme_id")
            tids = [single] if single else ["other"]

        seen_in_comment: set[str] = set()
        for raw_tid in tids:
            tid = str(raw_tid) if raw_tid else "other"
            if tid not in theme_ids:
                tid = "other"
            if tid in seen_in_comment:
                continue  # 同一评论对同一主题不重复计数
            seen_in_comment.add(tid)

            counts[tid]["total"] += 1
            counts[tid][sentiment] += 1

            if (
                tid != "other"
                and item.get("is_quote_candidate")
                and item.get("original")
            ):
                bucket = sentiment if sentiment in ("positive", "negative") else "neutral"
                pool = quotes_pool[tid][bucket]
                if len(pool) < 5:
                    pool.append((item.get("translation", ""), item.get("original", "")))

    total_classified = comment_count

    # 整体情感分布（按评论计，mixed 归入中性侧，和≈100%）
    overall_base = comment_count or 1
    sentiment_overall = {
        "positive_pct": round(overall["positive"] / overall_base * 100, 1),
        "negative_pct": round(overall["negative"] / overall_base * 100, 1),
        "neutral_pct": round(
            (overall["neutral"] + overall["mixed"]) / overall_base * 100, 1
        ),
    }

    themes_out: list[dict] = []
    other_themes_out: list[dict] = []
    # 主题占比基数 = 评论总数（多标签下各主题占比之和可能 >100%）
    base = comment_count or 1

    for t in final_themes:
        tid = t["id"]
        c = counts[tid]
        cnt = c["total"]
        pct = round(cnt / base * 100, 1)
        denom = cnt or 1

        if pct < other_pct:
            other_themes_out.append({"name": t["name"], "count": cnt, "percentage": pct})
            continue

        pool = quotes_pool[tid]
        # 每种情感各取 1 条代表，凑 2~3 条
        quotes: list[str] = []
        for bucket in ("positive", "negative", "neutral"):
            for translation, original in pool[bucket][:1]:
                quotes.append(format_quote(translation, original))
        # 不足 2 条时再补
        if len(quotes) < 2:
            for bucket in ("positive", "negative", "neutral"):
                for translation, original in pool[bucket][1:]:
                    quotes.append(format_quote(translation, original))
                    if len(quotes) >= 3:
                        break
                if len(quotes) >= 3:
                    break
        quotes = quotes[:3]

        themes_out.append({
            "id": tid,
            "name": t["name"],
            "description": t.get("description", ""),
            "count": cnt,
            "percentage": pct,
            "positive_count": c["positive"],
            "positive_pct": round(c["positive"] / denom * 100, 1),
            "negative_count": c["negative"],
            "negative_pct": round(c["negative"] / denom * 100, 1),
            "neutral_count": c["neutral"] + c["mixed"],
            "neutral_pct": round((c["neutral"] + c["mixed"]) / denom * 100, 1),
            "quotes": quotes,
        })

    themes_out.sort(key=lambda x: x["count"], reverse=True)
    other_themes_out.sort(key=lambda x: x["count"], reverse=True)

    return {
        "total_classified": total_classified,
        "sentiment_overall": sentiment_overall,
        "themes": themes_out,
        "other_themes": other_themes_out,
    }
