"""Three-duty audit contracts. Structural completion is not semantic certification."""
from copy import deepcopy
import hashlib
import json
import re
from app.services.report_postwrite_inputs import report_blocks

LEGACY_SOURCE_UNIT_FORMAT = """
本批只检查targets；report提供原始段落上下文。开放题人数另按限定题目的数量任务核验，本批不猜人数，也不因未计数否定合理解释。
source_units每项是一道题的一份原回答，ref、player、question、original放在一起。相同player的不同ref可联合支持观点，但各自的释义仅描述该ref下的original；需要另一题的意思时，另列那一题ref及其释义，不把同一玩家的多题内容合写在一个ref下。
review_refs是要核对的材料，并不代表它一定支持观点。每个review_ref及实际使用的其他来源在该观点的sources列出一次：{ref,meaning,relation,part}。meaning是这一条原回答的简短中文原意，relation=direct|inference|unrelated|unclear，part说明它支持哪部分或不相关之处。客观选择和统计项meaning可为空。不重抄原文或题目；程序会按ref回填到同一项。不是要求每条来源支持全部分句，也不限制同一玩家跨题的合理解释。
checks每个目标一次，verdict=supported|inference|advice|issue|uncertain，inference和advice均可保留。reason简短即可。citation_issues仅列真实错引或实质缺失，格式{refs,reason,replacement_refs}；材料无关但报告未引用，不算错引。repair只对明确原意或事实错误给最小修改，其他为空。不把解释改成逐字复述。
JSON:{"status":"complete","checks":[{"id":"C001","verdict":"inference","reason":"简短依据","sources":[{"ref":"q1/r2","meaning":"此题实际原意","relation":"inference","part":"支持哪部分"}],"citation_issues":[],"repair":""}],"not_checked":[]}。只给结果和短依据，不输出过程推理。"""

SOURCE_UNIT_FORMAT = LEGACY_SOURCE_UNIT_FORMAT.replace('"not_checked":[]', '"not_checked":[],"scope_notes":[]') + """
备注分成两个字符串数组：not_checked只列本批targets中确实尚未核验的内容；scope_notes只列本批范围外的说明，例如其他报告段落、另批核验的开放题人数。范围外说明不代表本批漏检；不要把本批实际未核验内容放入scope_notes。其余判断标准不变。
"""

REGISTRY_FORMAT = """
只输出JSON: {"claims":[{"id":"C001","original":"报告原句","block_ids":["B0001"],"kind":"fact|inference|advice","review_refs":[],"count_task_ids":[]}],
"coverage":[{"block_id":"B0001","claim_ids":["C001"],"no_claim":false}],
"tasks":[{"id":"T001","predicate":"明确提及的条件","question_keys":["1"],"mode":"subjective|objective","exact_values":[],
"group":null,"population":"all|answered","reported_count":null,"reported_denominator":null,"reported_percent":null,"scope_text":"少数"}],
"registration_gaps":[],"verification_notes":[],"registration_notes":[]}。
三个备注字段均为字符串数组：registration_gaps只列尚未登记的报告内容；verification_notes列已经登记但证据、原意或人数尚待核验的提示；registration_notes列报告未表达、因此无需登记等说明。后两者不表示漏登记，也不表示核验通过。登记阶段不核验事实，不因缺证据而省略报告观点。不再用not_checked混装这些不同状态。
group如需限定只能用客观题 {"question_key":"0","values":["某选项"]}；无法表达的复杂口径写scope_unresolved=true并说明，不猜群体。
每项original必须为报告中连续原句；review_refs保留原句与邻近上下文的引用。tasks只登记实际人数范围表述，允许为空。
引用必须使用来源ref。Python统计以statistics_markdown核对，统计表不是原回答。不得自行给任务预填人数或自行决定最终分母。
"""


def parse_json(text):
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    value = json.loads(clean)
    if not isinstance(value, dict):
        raise ValueError("核验返回必须为JSON对象")
    return value


def messages(policy, payload, protocol=SOURCE_UNIT_FORMAT):
    return [{"role": "system", "content": policy + protocol},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def reference_key(ref):
    """Normalize only cell-like identifiers; resolution still requires real data."""
    if isinstance(ref, str):
        match = re.fullmatch(r"([A-Z]+)0*(\d+)", ref)
        if match:
            return match[1] + str(int(match[2]))
    return ref


def reference_lookup(units):
    lookup = {}
    for unit in units:
        key = reference_key(unit["ref"])
        if key in lookup and lookup[key]["ref"] != unit["ref"]:
            raise ValueError("原文编号别名冲突")
        lookup[key] = unit
    return lookup


def statistical_sources(packet):
    stats = packet.get("statistics")
    if not isinstance(stats, dict):
        return []
    return [{"ref": ref, "kind": "python_statistic", "original": json.dumps(value, ensure_ascii=False)}
            for ref, value in stats.items() if re.fullmatch(r"S\d+", ref)]


def registry_payload(report, packet):
    return {"report": report, "blocks": report_blocks(report), "questions": packet["questions"],
            "statistics_markdown": packet["statistics_markdown"],
            "source_index": [{k: u.get(k) for k in ("ref", "player", "question_key")} for u in packet["source_units"]],
            "statistic_refs": [u["ref"] for u in statistical_sources(packet)]}


def registry_notes(obj, *, raw_registry=None, legacy_note_dispositions=None):
    """Keep registration gaps separate; legacy notes require explicit, bound review."""
    fields = ("registration_gaps", "verification_notes", "registration_notes", "not_checked")
    notes = {key: deepcopy(obj.get(key) or []) for key in fields}
    if any(not isinstance(items, list) or any(not isinstance(s, str) for s in items)
           for items in notes.values()):
        raise ValueError("登记备注必须为字符串数组")
    errors = []
    legacy = notes["not_checked"]
    if legacy_note_dispositions is not None:
        mapping = legacy_note_dispositions
        if not isinstance(raw_registry, str) or not isinstance(mapping, dict) or mapping.get("source_response_sha256") != hashlib.sha256(raw_registry.encode("utf-8")).hexdigest():
            raise ValueError("旧备注分类与原始响应指纹不一致")
        entries = mapping.get("entries")
        if not isinstance(entries, list) or len(entries) != len(legacy):
            raise ValueError("旧备注分类没有逐条覆盖")
        destinations = {"pending_verification": "verification_notes", "not_applicable": "registration_notes",
                        "registration_gap": "registration_gaps"}
        for index, (entry, original) in enumerate(zip(entries, legacy)):
            if (not isinstance(entry, dict) or entry.get("index") != index or entry.get("original") != original
                    or entry.get("kind") not in destinations or not str(entry.get("reason") or "").strip()):
                raise ValueError("旧备注分类缺失或与原文不一致")
            notes[destinations[entry["kind"]]].append(original)
    elif legacy:
        errors.append("旧格式未检查项尚未归类，保留未完成状态")
    errors.extend("未登记：" + item for item in notes["registration_gaps"])
    return {"registration_gaps": notes["registration_gaps"], "verification_notes": notes["verification_notes"],
            "registration_notes": notes["registration_notes"], "legacy_not_checked": legacy,
            "note_dispositions": deepcopy(legacy_note_dispositions), "errors": errors}


def validate_registry(obj, report, packet, *, raw_registry=None, legacy_note_dispositions=None):
    blocks = {b["id"]: b["text"] for b in report_blocks(report)}
    lookup = reference_lookup(packet["source_units"] + statistical_sources(packet))
    columns = {q.get("key") for q in packet["questions"] if isinstance(q, dict)}
    claims, errors, seen = [], [], set()
    for claim in obj.get("claims") or []:
        ident = claim.get("id")
        if not isinstance(ident, str) or not ident or ident in seen:
            errors.append("观点编号缺失或重复")
            continue
        seen.add(ident)
        if not claim.get("original") or claim["original"] not in report:
            errors.append(f"{ident}:观点无法定位到初稿")
            continue
        block_ids = claim.get("block_ids") or []
        if (not block_ids or any(b not in blocks for b in block_ids)
                or claim["original"] not in "\n\n".join(blocks[b] for b in block_ids)):
            errors.append(f"{ident}:段落定位不一致")
            continue
        claim = deepcopy(claim)
        written_refs = re.findall(r"q\d+/r\d+|(?<![A-Za-z0-9_])[A-Z]+\d+(?![A-Za-z0-9_])", claim["original"])
        written_refs = [r for r in written_refs if r.startswith("q") or reference_key(r) in lookup
                        or re.sub(r"\d+$", "", r) in columns]
        refs = (claim.get("review_refs") or []) + written_refs
        claim["review_refs"] = list(dict.fromkeys(lookup.get(reference_key(r), {}).get("ref", r) for r in refs))
        claims.append(claim)
    valid = {c["id"]: c for c in claims}
    covered = set()
    for item in obj.get("coverage") or []:
        block = item.get("block_id")
        ids = item.get("claim_ids") or []
        if block not in blocks or block in covered:
            errors.append("段落登记缺失或重复")
        elif ids and all(i in valid and block in valid[i]["block_ids"] for i in ids):
            covered.add(block)
        elif item.get("no_claim") is True and not ids and not any(block in c["block_ids"] for c in claims):
            covered.add(block)
        else:
            errors.append(f"{block}:段落未完成登记")
    errors.extend(f"{b}:段落未登记" for b in blocks.keys() - covered)
    tasks, tids = [], set()
    for task in obj.get("tasks") or []:
        tid = task.get("id")
        if not isinstance(tid, str) or not tid or tid in tids:
            errors.append("人数任务编号缺失或重复")
            continue
        tids.add(tid)
        tasks.append(deepcopy(task))
    for claim in claims:
        if set(claim.get("count_task_ids") or []) - tids:
            errors.append(f"{claim['id']}:人数任务缺失")
    notes = registry_notes(obj, raw_registry=raw_registry, legacy_note_dispositions=legacy_note_dispositions)
    errors.extend(notes.pop("errors"))
    return {"claims": claims, "tasks": tasks, **notes, "errors": errors, "complete": not errors}


def meaning_payload(report, packet, claims):
    return {"report": report, "research_task": packet.get("research_task", {}), "questions": packet["questions"],
            "statistics_markdown": packet.get("statistics_markdown", ""), "statistics": packet.get("statistics", {}),
            "source_units": packet["source_units"], "targets": claims}


def bind_meanings(obj, payload):
    units = reference_lookup(payload["source_units"] + statistical_sources(payload))
    expected = {c["id"]: c for c in payload["targets"]}
    checks, errors, seen = [], [], set()
    for raw in obj.get("checks") or []:
        ident = raw.get("id")
        if ident not in expected or ident in seen:
            errors.append("未知或重复观点编号")
            continue
        seen.add(ident)
        check = deepcopy(raw)
        check["original"] = expected[ident]["original"]
        bound, refs, local = [], set(), []
        if raw.get("verdict") not in {"supported", "inference", "advice", "issue", "uncertain"}:
            local.append("观点结论格式不完整")
        for item in raw.get("sources") or []:
            key = reference_key(item.get("ref"))
            ref = units.get(key, {}).get("ref", item.get("ref"))
            if key not in units or ref in refs:
                local.append(f"无法定位或重复来源:{ref}")
                continue
            refs.add(ref)
            unit = units[key]
            if item.get("relation") not in {"direct", "inference", "unrelated", "unclear"}:
                local.append(f"来源关系缺失:{ref}")
            if unit.get("kind") == "open_text" and not str(item.get("meaning") or "").strip():
                local.append(f"原意未核验:{ref}")
            # Ignore any model-generated original/player/question fields.
            bound.append({"ref": ref, "meaning": str(item.get("meaning") or ""), "relation": item.get("relation"),
                          "part": str(item.get("part") or ""), "source": deepcopy(unit)})
        expected_refs = {units.get(reference_key(r), {}).get("ref", r) for r in expected[ident].get("review_refs") or []}
        missing = expected_refs - refs
        if missing:
            local.append("未核对指定来源:" + ",".join(sorted(missing)))
        check["sources"] = bound
        check["errors"] = local
        check["complete"] = not local
        checks.append(check)
        errors.extend(f"{ident}:{e}" for e in local)
    missing = list(expected.keys() - seen)
    errors.extend(f"{i}:观点未核验" for i in missing)
    notes = obj.get("not_checked", [])
    declared_scope = obj.get("scope_notes", [])
    if any(not isinstance(items, list) or any(not isinstance(n, str) for n in items)
           for items in (notes, declared_scope)):
        raise ValueError("原意备注必须为字符串数组")
    def target_gap(note):
        return any(re.search(r"(?<![A-Za-z0-9])" + re.escape(i)
                             + r"(?![A-Za-z0-9])[：:\s]*(?:尚|仍|还|暂)?未(?:核验|核实|检查|核对|完成)", note)
                   for i in expected)
    # Explicit notes on out-of-scope report paragraphs are informational.
    def outside_scope(note):
        if target_gap(note):
            return False
        # A neighboring paragraph may mention a target ID as context. Explicitly
        # saying it is outside this batch is not an unchecked target declaration.
        if any(phrase in note for phrase in ("未作为本批目标", "本批目标之外", "非targets段落")):
            return True
        if re.fullmatch(r"\s*(?:报告中)?(?:targets|本批目标)(?:以外|之外)的?(?:报告)?段落[。；;\s]*", note):
            return True
        return (not any(i in note for i in expected)
                and bool(re.search(r"(?:开放题人数|数量统计|人数统计).*(?:本批|另行|另批|不在|未核验)", note)))
    relevant_notes = [n for n in notes if not outside_scope(n)] + [n for n in declared_scope if target_gap(n)]
    if relevant_notes:
        errors.append("核验员留下未检查项")
    if relevant_notes or "未知或重复观点编号" in errors:
        for check in checks:
            check["complete"] = False
    return {"checks": checks, "errors": errors, "missing": missing, "complete": not errors,
            "scope_notes": [n for n in notes if outside_scope(n)] + [n for n in declared_scope if not target_gap(n)],
            "not_checked": relevant_notes, "legacy_not_checked": notes}


def option_values(values, question):
    """Resolve only this question's declared option aliases, without guessing."""
    definition = question.get("definition") or {}
    options = definition.get("options")
    values = {str(value) for value in values}
    if isinstance(options, dict) and options:
        pairs = [(str(raw), str(display)) for raw, display in options.items()]
        canonical = {display for _, display in pairs}
    elif isinstance(options, list) and options:
        canonical = {str(value) for value in options}
        pairs = []
        aliases = definition.get("value_aliases") or {}
        if not isinstance(aliases, dict):
            raise ValueError("该题选项映射格式无效")
        for display, variants in aliases.items():
            if display not in canonical or not isinstance(variants, list):
                raise ValueError("该题选项映射不属于已确认选项")
            pairs.extend((str(raw), display) for raw in variants)
    else:
        return values  # Legacy packets without declared options use exact values.
    mapping = {display: display for display in canonical}
    for raw, display in pairs:
        if raw in mapping and mapping[raw] != display:
            raise ValueError("该题选项映射冲突，不能猜测人数")
        mapping[raw] = display
    if values - mapping.keys():
        raise ValueError("计数选项不在该题已有映射中，不能猜测人数")
    return {mapping[value] for value in values}


def count_method(task):
    """Only explicit deterministic operations bypass semantic membership checks."""
    method = task.get("count_method")
    if method is not None:
        if method not in {"selection", "nonempty", "semantic"}:
            raise ValueError("未知人数统计类型")
        return method
    if task.get("mode") == "objective":
        return "selection"
    # Backward-compatible, narrow declarative form; never infer from a task ID
    # or from words like '有效回答'/'表达意见', which require semantic judgment.
    if re.fullmatch(r'(?:在[“\"].+?[”\"]题中)?填写了非空回答', task.get("predicate") or ""):
        return "nonempty"
    return "semantic"


def count_scope(task, packet):
    keys = set(task.get("question_keys") or [])
    questions = {q["key"]: q for q in packet["questions"] if q.get("selected")}
    if not keys or keys - questions.keys() or task.get("scope_unresolved"):
        raise ValueError("计数题目或群体范围未确定")
    if task.get("population") not in {"all", "answered"} or task.get("mode") not in {"subjective", "objective"}:
        raise ValueError("计数分母或类型未确定")
    players = set(packet["players"])
    group = task.get("group")
    if group:
        key = group.get("question_key")
        if key not in questions or questions[key]["type"] not in {"single_choice", "scale"} or not group.get("values"):
            raise ValueError("群体筛选仅接受已确认的客观单选值")
        values = option_values(group["values"], questions[key])
        players &= {u["player"] for u in packet["source_units"] if u["question_key"] == key
                    and option_values([u["original"]], questions[key]) & values}
    units = [deepcopy(u) for u in packet["source_units"] if u["question_key"] in keys and u["player"] in players]
    if task["population"] == "answered":
        players &= {u["player"] for u in units}
    method = count_method(task)
    if method == "selection" and (any(questions[k]["type"] not in {"single_choice", "scale"} for k in keys)
                                           or not task.get("exact_values")):
        raise ValueError("该客观人数口径应对照Python统计表，不能猜测拆分选项")
    if method == "selection":
        for key in keys:
            option_values(task["exact_values"], questions[key])
    return sorted(players), units


def deterministic_count(task, players, units, packet):
    method = count_method(task)
    if method == "nonempty":
        matches = {u["player"] for u in units if u.get("original") is not None and str(u["original"]).strip()}
    elif method == "selection":
        questions = {q["key"]: q for q in packet["questions"]}
        values = {k: option_values(task["exact_values"], questions[k]) for k in task["question_keys"]}
        matches = {u["player"] for u in units
                   if option_values([u["original"]], questions[u["question_key"]]) & values[u["question_key"]]}
    else:
        raise ValueError("语义提及人数不能用确定性统计猜测")
    result = aggregate_count(task, players, [{"player": p, "count": p in matches, "sources": []} for p in players])
    result["count_method"] = method
    return result


COUNT_FORMAT = """
只核本批task指定题目的明确提及人数，source_units已由程序限定题目及群体。不要借用同一玩家其他题目计数。
每个players中的玩家一次；命中predicate才count=true，合理解释不能当作玩家明说。未提及或无回答为false，歧义为null。
JSON:{"members":[{"player":"P00002","count":true,"sources":[{"ref":"q1/r2","meaning":"此原文意思","relation":"direct","part":"满足哪部分条件"}]}]}。
非命中可不列sources。不得输出总人数或比例，Python去重汇总。"""


def bind_members(obj, task, players, units):
    source = reference_lookup(units)
    members, errors, ambiguities, seen = [], [], [], set()
    for raw in obj.get("members") or []:
        player = raw.get("player")
        if player not in players or player in seen:
            errors.append("未知或重复计数玩家")
            continue
        seen.add(player)
        value = raw.get("count")
        if value is None and "count" in raw:
            ambiguities.append(f"{player}:计数有歧义")
        elif type(value) is not bool:
            errors.append(f"{player}:计数判断缺失或无效")
        bound = []
        for item in raw.get("sources") or []:
            key = reference_key(item.get("ref"))
            ref = source.get(key, {}).get("ref", item.get("ref"))
            if key not in source or source[key]["player"] != player:
                errors.append(f"{player}:计数来源错位")
                continue
            if not str(item.get("meaning") or "").strip():
                errors.append(f"{player}:主观计数缺少原意")
            bound.append({"ref": ref, "meaning": str(item.get("meaning") or ""), "relation": item.get("relation"),
                          "part": str(item.get("part") or ""), "source": deepcopy(source[key])})
        if value is True and (not bound or not any(b["relation"] == "direct" for b in bound)):
            errors.append(f"{player}:明确提及缺少直接来源")
        members.append({"player": player, "count": value, "sources": bound})
    errors.extend(f"{p}:未核验人数" for p in set(players) - seen)
    return {"members": members, "errors": errors, "ambiguities": ambiguities,
            "reviewed": not errors, "complete": not errors}


def aggregate_count(task, players, members):
    by_player = {m["player"]: m for m in members if m["player"] in players}
    count = sum(m["count"] is True for m in by_player.values())
    denominator = len(set(players))
    uncertain = [m["player"] for m in by_player.values() if m.get("count") is None]
    reviewed = len(by_player) == denominator and all("count" in m and (type(m["count"]) is bool or m["count"] is None)
                                                   for m in by_player.values())
    exact = reviewed and not uncertain
    upper = count + len(uncertain)
    percent = count * 100 / denominator if denominator else 0
    upper_percent = upper * 100 / denominator if denominator else 0
    issues = []
    if reviewed:
        reported = task.get("reported_count")
        if reported is not None and not count <= reported <= upper:
            issues.append(f"reported_count:报告为{reported}，Python核算为{count}" if exact else
                          f"reported_count:报告为{reported}，不在已核验人数范围{count}—{upper}内")
        if task.get("reported_denominator") is not None and task["reported_denominator"] != denominator:
            issues.append(f"reported_denominator:报告为{task['reported_denominator']}，Python核算为{denominator}")
        if task.get("reported_percent") is not None and not percent - 1 <= float(task["reported_percent"]) <= upper_percent + 1:
            issues.append(f"报告比例与Python核算{percent:.1f}%不一致" if exact else
                          f"报告比例不在已核验范围{percent:.1f}%—{upper_percent:.1f}%内")
    scope = task.get("scope_text") or ""
    def valid(n):
        p = n * 100 / denominator if denominator else 0
        return {"个别": 1 <= n <= 2 and p < 50, "少数": n > 2 and p < 50,
                "过半": 50 < p < 80, "多数": p > 50, "大多数": p > 50,
                "绝大多数": 80 <= p < 90, "几乎所有": 90 <= p < 100, "所有": p == 100, "全部": p == 100}
    if reviewed and scope in valid(count) and not any(valid(n)[scope] for n in range(count, upper + 1)):
        issues.append(f"“{scope}”与{count}/{denominator}不符" if exact else f"“{scope}”与人数范围{count}—{upper}/{denominator}不符")
    return {"id": task["id"], "task": deepcopy(task), "count": count if exact else None,
            "denominator": denominator, "percent": round(percent, 2) if exact else None,
            "count_min": count if reviewed else None, "count_max": upper if reviewed else None,
            "uncertain_players": uncertain, "reviewed": reviewed, "exact": exact,
            "review_status": "unreviewed" if not reviewed else ("reviewed" if exact else "ambiguous"),
            "complete": exact, "issues": issues, "members": list(by_player.values())}
