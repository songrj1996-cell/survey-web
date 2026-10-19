"""Meaning binding and deterministic counts; no live model requests."""
from copy import deepcopy
import hashlib
import json
import unittest
from app.services.report_postwrite_inputs import freeze_inputs, validate_frozen, report_blocks
from app.services.report_postwrite_audit import (bind_meanings, count_scope, bind_members, aggregate_count,
    validate_registry, meaning_payload, option_values)


def session():
    return {"rows": [["id", "体验", "建议", "评分"], ["same", "卡牌有趣", "希望常驻", "好"],
                     ["same", "", "字太小", "差"], ["third", "规则熟悉", "", "好"]],
            "confirmed_columns": [{"column_indexes": [0], "role": "id", "name": "id"},
                {"column_indexes": [1], "role": "open_text", "name": "体验"},
                {"column_indexes": [2], "role": "open_text", "name": "建议"},
                {"column_indexes": [3], "role": "single_choice", "name": "评分"}],
            "report_mode": "insight", "mode": "standard", "plan": {"parts": []}, "qualitative_context": {"goal": "了解体验"}}


def packet():
    return freeze_inputs(session())["postwrite_packet"]


def claim(ident="C1", ref="q1/r2"):
    return {"id": ident, "original": "熟悉规则可能降低门槛", "kind": "inference", "review_refs": [ref]}


def response(target):
    return {"id": target["id"], "verdict": "inference", "reason": "基于原回答的合理解释", "sources": [
        {"ref": r, "meaning": "此题的原意", "relation": "inference", "part": "支持解释"} for r in target["review_refs"]],
        "citation_issues": [], "repair": ""}


def note_review(raw, kinds=None):
    from app.services.report_postwrite_audit import parse_json
    items = parse_json(raw).get("not_checked", [])
    return {"source_response_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "entries": [{"index": i, "original": text, "kind": (kinds or ["pending_verification"] * len(items))[i],
                         "reason": "合成测试中已登记，留待核验"} for i, text in enumerate(items)]}


class CountStateTests(unittest.TestCase):
    def test_confirmed_list_aliases_drive_both_counts_and_population(self):
        from app.services.report_postwrite_audit import deterministic_count
        s = session()
        s["rows"][1][3], s["rows"][2][3], s["rows"][3][3] = "Very fun", "Biasa Saja", "Sangat Menyenangkan"
        s["confirmed_columns"][3].update(options=["非常有趣", "一般"],
            value_aliases={"非常有趣": ["Very fun", "Sangat Menyenangkan"], "一般": ["Biasa Saja"]})
        p = freeze_inputs(s)["postwrite_packet"]
        original = deepcopy(p["source_units"])
        task = {"id": "T", "mode": "objective", "question_keys": ["3"], "population": "all", "exact_values": ["非常有趣"]}
        players, units = count_scope(task, p)
        result = deterministic_count(task, players, units, p)
        self.assertEqual((result["count"], result["denominator"]), (2, 3))
        grouped = {**task, "group": {"question_key": "3", "values": ["Very fun"]}}
        players, units = count_scope(grouped, p)
        self.assertEqual(deterministic_count(grouped, players, units, p)["count"], 2)
        self.assertEqual(len(players), 2)
        self.assertEqual(p["source_units"], original)

    def test_declared_aliases_never_guess_missing_or_conflicting_options(self):
        definition = {"options": ["好", "差"], "value_aliases": {"好": ["Good"]}}
        self.assertEqual(option_values(["Good", "好"], {"definition": definition}), {"好"})
        for invalid in [definition, {"options": ["好", "差"], "value_aliases": {"好": ["Good"], "差": ["Good"]}},
                        {"options": ["好"], "value_aliases": {"差": ["Bad"]}}]:
            with self.assertRaises(ValueError):
                option_values(["Unknown" if invalid == definition else "Good"], {"definition": invalid})
        self.assertEqual(option_values(["Good", "好"], {"definition": {"options": {"Good": "好"}}}), {"好"})

    def test_nonempty_is_not_content_quality_and_deduplicates_players(self):
        from app.services.report_postwrite_audit import deterministic_count, count_method
        p = packet()
        task = {"id": "arbitrary-id", "predicate": "填写了非空回答", "mode": "subjective",
                "question_keys": ["1"], "population": "all"}
        values = ["None", "没有补充", " ", None, 0]
        p["players"] = [f"p{i}" for i in range(5)]
        p["source_units"] = [{"ref": f"r{i}", "player": f"p{i}", "question_key": "1", "original": value}
                             for i, value in enumerate(values)]
        p["source_units"].append({**p["source_units"][0], "ref": "duplicate"})
        players, units = count_scope(task, p)
        result = deterministic_count(task, players, units, p)
        self.assertEqual(result["count"], 3)
        self.assertTrue(result["exact"])
        self.assertEqual(count_method({**task, "predicate": "明确表达实质意见的非空回答"}), "semantic")
        self.assertEqual(count_method({**task, "predicate": "在“其他反馈”题中填写了非空回答"}), "nonempty")

    def test_ambiguity_is_reviewed_but_not_an_exact_number(self):
        task = {"id": "T1", "reported_count": 2, "scope_text": "多数"}
        result = aggregate_count(task, ["a", "b", "c"], [
            {"player": "a", "count": True}, {"player": "b", "count": None}, {"player": "c", "count": False}])
        self.assertTrue(result["reviewed"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["review_status"], "ambiguous")
        self.assertIsNone(result["count"])
        self.assertIsNone(result["percent"])
        self.assertEqual((result["count_min"], result["count_max"]), (1, 2))
        self.assertEqual(result["issues"], [])
        outside = aggregate_count({**task, "reported_count": 3}, ["a", "b", "c"], result["members"])
        self.assertEqual(len(outside["issues"]), 1)

    def test_null_is_distinct_from_absent_or_invalid_judgment(self):
        task = {"id": "T1", "question_keys": ["1"], "mode": "subjective", "population": "all"}
        for answer, reviewed in (({"count": None}, True), ({}, False), ({"count": 0}, False)):
            bound = bind_members({"members": [{"player": "p", **answer}]}, task, ["p"], [])
            self.assertEqual(bound["reviewed"], reviewed)
        missing = aggregate_count(task, ["p", "q"], [{"player": "p", "count": None}])
        self.assertFalse(missing["reviewed"])
        self.assertEqual(missing["review_status"], "unreviewed")
        self.assertEqual(missing["issues"], [])

    def test_ambiguity_does_not_hide_wrong_question_or_duplicate_player(self):
        task = {"id": "T1", "question_keys": ["1"], "mode": "subjective", "population": "all"}
        players, units = count_scope(task, packet())
        invalid = {"player": "P00002", "count": None, "sources": [{"ref": "q2/r2", "meaning": "其他题", "relation": "ambiguous"}]}
        self.assertFalse(bind_members({"members": [invalid]}, task, ["P00002"], units)["reviewed"])
        self.assertFalse(bind_members({"members": [{"player": "p", "count": None}] * 2}, task, ["p"], [])["reviewed"])


class RegistryStateTests(unittest.TestCase):
    def fixture(self):
        report = "熟悉规则可能降低门槛 [q1/r4]"
        obj = {"claims": [{"id": "C1", "original": report, "block_ids": ["B0001"], "count_task_ids": []}],
               "coverage": [{"block_id": "B0001", "claim_ids": ["C1"]}], "tasks": []}
        return report, obj

    def test_pending_notes_do_not_block_registration(self):
        report, obj = self.fixture()
        obj.update(verification_notes=["原意尚未核验"], registration_notes=["报告未写此分项，无需登记"])
        result = validate_registry(obj, report, packet())
        self.assertTrue(result["complete"])
        self.assertEqual(result["verification_notes"], obj["verification_notes"])
        self.assertEqual(result["registration_notes"], obj["registration_notes"])

    def test_explicit_registration_gap_still_blocks(self):
        report, obj = self.fixture()
        obj["registration_gaps"] = ["本段另一条观点尚未登记"]
        self.assertFalse(validate_registry(obj, report, packet())["complete"])

    def test_notes_cannot_hide_structural_errors(self):
        report, original = self.fixture()
        for change in (lambda o: o.update(coverage=[]), lambda o: o["claims"][0].update(original="不存在的句子"),
                       lambda o: o["claims"].append(deepcopy(o["claims"][0])),
                       lambda o: o["claims"][0].update(count_task_ids=["missing"])):
            obj = deepcopy(original)
            obj["verification_notes"] = ["尚未核验"]
            change(obj)
            self.assertFalse(validate_registry(obj, report, packet())["complete"])

    def test_legacy_note_requires_explicit_bound_review(self):
        report, obj = self.fixture()
        obj["not_checked"] = ["已经登记但未核验", "报告未写，不登记"]
        raw = json.dumps(obj, ensure_ascii=False)
        self.assertFalse(validate_registry(obj, report, packet())["complete"])
        review = note_review(raw, ["pending_verification", "not_applicable"])
        result = validate_registry(obj, report, packet(), raw_registry=raw, legacy_note_dispositions=review)
        self.assertTrue(result["complete"])
        self.assertEqual(result["legacy_not_checked"], obj["not_checked"])
        self.assertEqual(result["verification_notes"] + result["registration_notes"], obj["not_checked"])
        self.assertEqual(result["note_dispositions"], review)

    def test_legacy_real_gap_remains_blocking(self):
        report, obj = self.fixture()
        obj["not_checked"] = ["这条观点没有登记"]
        raw = json.dumps(obj, ensure_ascii=False)
        result = validate_registry(obj, report, packet(), raw_registry=raw,
                                   legacy_note_dispositions=note_review(raw, ["registration_gap"]))
        self.assertFalse(result["complete"])

    def test_wrong_hash_missing_or_replaced_note_cannot_be_reclassified(self):
        report, obj = self.fixture()
        obj["not_checked"] = ["待核验"]
        raw = json.dumps(obj, ensure_ascii=False)
        for change in (lambda m: m.update(source_response_sha256="bad"), lambda m: m.update(entries=[]),
                       lambda m: m["entries"][0].update(original="另一条"),
                       lambda m: m["entries"][0].update(kind="unknown")):
            review = note_review(raw); change(review)
            with self.assertRaises(ValueError):
                validate_registry(obj, report, packet(), raw_registry=raw, legacy_note_dispositions=review)


class AuditTests(unittest.TestCase):
    def mapped_packet(self):
        p = packet()
        next(q for q in p["questions"] if q["key"] == "3")["definition"]["options"] = {"Good": "好", "Baik": "好", "Bad": "差"}
        return p

    def test_option_aliases_use_same_question_mapping_and_preserve_inputs(self):
        p = self.mapped_packet(); before = deepcopy(p)
        question = next(q for q in p["questions"] if q["key"] == "3")
        self.assertEqual(option_values(["Good", "Baik", "好"], question), {"好"})
        task = {"mode": "objective", "question_keys": ["3"], "population": "all", "exact_values": ["Good"],
                "group": {"question_key": "3", "values": ["Baik"]}}
        players, units = count_scope(task, p)
        self.assertEqual(players, ["P00002", "P00004"])
        self.assertTrue(all(u["question_key"] == "3" for u in units))
        self.assertEqual(p, before)
        self.assertEqual(task["group"]["values"], ["Baik"])

    def test_unknown_mapped_option_does_not_silently_become_zero(self):
        p = self.mapped_packet()
        task = {"mode": "objective", "question_keys": ["3"], "population": "all", "exact_values": ["good"]}
        with self.assertRaisesRegex(ValueError, "映射"):
            count_scope(task, p)
        task.update(exact_values=["Good"], group={"question_key": "3", "values": ["Other"]})
        with self.assertRaisesRegex(ValueError, "映射"):
            count_scope(task, p)

    def test_subjective_group_uses_objective_alias_without_changing_question_scope(self):
        p = self.mapped_packet()
        task = {"mode": "subjective", "question_keys": ["1"], "population": "all",
                "group": {"question_key": "3", "values": ["Good", "Baik", "好"]}}
        players, units = count_scope(task, p)
        self.assertEqual(players, ["P00002", "P00004"])
        self.assertEqual({u["question_key"] for u in units}, {"1"})

    def test_legacy_cell_aliases_and_statistics_preserve_exact_sources(self):
        p = packet()
        first, second = deepcopy(p["source_units"][0]), deepcopy(p["source_units"][3])
        first.update(ref="H5", question_key="H")
        second.update(ref="I5", question_key="I", player=first["player"], original="字太小")
        p["source_units"] = [first, second]
        p["statistics"] = {"S01": {"n": 2, "denominator": 3}}
        report = "体验判断 [H05,I5,S01]"
        reg = validate_registry({"claims": [{"id": "C1", "original": report, "block_ids": ["B0001"]}],
            "coverage": [{"block_id": "B0001", "claim_ids": ["C1"]}]}, report, p)
        target = reg["claims"][0]
        self.assertEqual(target["review_refs"], ["H5", "I5", "S01"])
        answer = response(target)
        answer["sources"][0]["ref"] = "H005"
        answer["sources"][2]["meaning"] = ""
        result = bind_meanings({"checks": [answer]}, meaning_payload(report, p, [target]))
        self.assertTrue(result["complete"], result)
        bound = result["checks"][0]["sources"]
        self.assertEqual(bound[0]["source"]["original"], first["original"])
        self.assertEqual(bound[1]["source"]["original"], "字太小")
        self.assertEqual(bound[2]["source"]["kind"], "python_statistic")
        self.assertNotIn("player", bound[2]["source"])

    def test_legacy_count_alias_does_not_allow_wrong_question_or_player(self):
        units = [{"ref": "H5", "player": "P005", "original": "有趣"}]
        for ref, player, expected in (("H05", "P005", True), ("I05", "P005", False), ("H05", "P006", False)):
            answer = {"members": [{"player": player, "count": True, "sources": [{"ref": ref, "meaning": "有趣", "relation": "direct"}]}]}
            result = bind_members(answer, {}, [player], units)
            self.assertEqual(result["complete"], expected)

    def test_unknown_cell_does_not_resolve_to_neighbor(self):
        p = packet()
        p["source_units"][0]["ref"] = "H5"
        target = claim(ref="H6")
        result = bind_meanings({"checks": [response(target)]}, meaning_payload("初稿", p, [target]))
        self.assertFalse(result["complete"])

    def test_physical_source_binding_with_blank_cells_and_duplicate_display_ids(self):
        p = packet()
        units = {u["ref"]: u for u in p["source_units"]}
        self.assertNotIn("q1/r3", units)
        self.assertEqual(units["q2/r3"]["original"], "字太小")
        self.assertNotEqual(units["q1/r2"]["player"], units["q2/r3"]["player"])
        self.assertEqual(units["q1/r2"]["player"], units["q2/r2"]["player"])
        self.assertEqual(len(p["players"]), 3)
        self.assertTrue(p["statistics_markdown"])

    def test_frozen_tampering_stops(self):
        frozen = freeze_inputs(session())
        frozen["postwrite_packet"]["source_units"][0]["original"] = "changed"
        with self.assertRaises(ValueError):
            validate_frozen(frozen)

    def test_sources_backfilled_not_model_copied(self):
        payload = meaning_payload("初稿", packet(), [claim()])
        answer = response(claim())
        answer["sources"][0].update(source={"original": "伪造"}, original="伪造", player="someone")
        result = bind_meanings({"checks": [answer]}, payload)
        self.assertTrue(result["complete"])
        self.assertEqual(result["checks"][0]["sources"][0]["source"]["original"], "卡牌有趣")

    def test_same_player_two_answers_stay_separate(self):
        target = {**claim(), "review_refs": ["q1/r2", "q2/r2"]}
        result = bind_meanings({"checks": [response(target)]}, meaning_payload("初稿", packet(), [target]))
        self.assertEqual([x["source"]["original"] for x in result["checks"][0]["sources"]], ["卡牌有趣", "希望常驻"])

    def test_unknown_missing_and_duplicate_source_are_incomplete(self):
        for refs in (["bad"], [], ["q1/r2", "q1/r2"]):
            answer = response({**claim(), "review_refs": refs})
            self.assertFalse(bind_meanings({"checks": [answer]}, meaning_payload("初稿", packet(), [claim()]))["complete"])

    def test_inference_and_advice_are_not_rejected(self):
        for verdict in ("inference", "advice", "issue"):
            answer = {**response(claim()), "verdict": verdict}
            result = bind_meanings({"checks": [answer]}, meaning_payload("初稿", packet(), [claim()]))
            self.assertTrue(result["complete"])
            self.assertEqual(result["checks"][0]["verdict"], verdict)

    def test_objective_needs_no_player_explanation(self):
        target = claim(ref="q3/r2")
        answer = response(target)
        answer["sources"][0]["meaning"] = ""
        self.assertTrue(bind_meanings({"checks": [answer]}, meaning_payload("初稿", packet(), [target]))["complete"])

    def test_unchecked_claim_cannot_be_complete(self):
        result = bind_meanings({"checks": []}, meaning_payload("初稿", packet(), [claim()]))
        self.assertEqual(result["missing"], ["C1"])
        self.assertFalse(result["complete"])

    def test_outside_scope_note_is_informational(self):
        answer = {"checks": [response(claim())], "not_checked": ["报告中非targets段落不在本次检查范围"]}
        self.assertTrue(bind_meanings(answer, meaning_payload("初稿", packet(), [claim()]))["complete"])

    def test_split_scope_notes_and_legacy_response_preserve_information(self):
        payload = meaning_payload("初稿", packet(), [claim()])
        legacy = ["开放题人数计数任务（T022等）按要求另行核验，本批未核", "targets以外的报告段落"]
        result = bind_meanings({"checks": [response(claim())], "not_checked": legacy}, payload)
        self.assertTrue(result["complete"])
        self.assertEqual(result["scope_notes"], legacy)
        self.assertEqual(result["legacy_not_checked"], legacy)
        declared = {"checks": [response(claim())], "scope_notes": ["其他章节由后面的批次处理"], "not_checked": []}
        self.assertTrue(bind_meanings(declared, payload)["complete"])
        declared["not_checked"] = ["有一条目标的原意还没有核实"]
        self.assertFalse(bind_meanings(declared, payload)["complete"])

    def test_scope_notes_cannot_hide_target_gap_or_missing_source(self):
        payload = meaning_payload("初稿", packet(), [claim()])
        for field in ("not_checked", "scope_notes"):
            result = bind_meanings({"checks": [response(claim())], field: ["targets以外的报告段落；C1未核验"]}, payload)
            self.assertFalse(result["complete"])
        for checks in ([], [{**response(claim()), "sources": []}]):
            self.assertFalse(bind_meanings({"checks": checks, "scope_notes": ["其他章节"]}, payload)["complete"])
        for value in ("其他章节", [None], None):
            with self.assertRaises(ValueError):
                bind_meanings({"checks": [response(claim())], "scope_notes": value}, payload)

    def test_count_scope_only_selected_question(self):
        task = {"id": "T1", "question_keys": ["1"], "mode": "subjective", "population": "all"}
        players, units = count_scope(task, packet())
        self.assertEqual(len(players), 3)
        self.assertTrue(all(u["question_key"] == "1" for u in units))
        players2, _ = count_scope({**task, "population": "answered"}, packet())
        self.assertEqual(len(players2), 2)

    def test_neighbor_context_and_deferred_counts_are_notes_not_missing_targets(self):
        payload = meaning_payload("初稿", packet(), [claim()])
        answer = {"checks": [response(claim())], "not_checked": [
            "C1上下文里的邻近句未作为本批目标核验", "开放题人数按本批规则未核验"]}
        result = bind_meanings(answer, payload)
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["scope_notes"]), 2)
        answer["not_checked"] = ["C1尚未核实"]
        self.assertFalse(bind_meanings(answer, payload)["complete"])
        answer["checks"] = []
        answer["not_checked"] = ["开放题人数按本批规则未核验"]
        self.assertFalse(bind_meanings(answer, payload)["complete"])

    def test_group_population_python_filter(self):
        task = {"id": "T1", "question_keys": ["2"], "mode": "subjective", "population": "all", "group": {"question_key": "3", "values": ["好"]}}
        players, units = count_scope(task, packet())
        self.assertEqual(players, ["P00002", "P00004"])
        self.assertEqual([u["ref"] for u in units], ["q2/r2"])

    def test_count_rejects_cross_question_and_other_player_source(self):
        task = {"id": "T1", "question_keys": ["1"], "mode": "subjective", "population": "all"}
        players, units = count_scope(task, packet())
        for ref in ("q2/r2", "q1/r4"):
            result = bind_members({"members": [{"player": "P00002", "count": True, "sources": [{"ref": ref, "meaning": "愿望", "relation": "direct"}]}]}, task, ["P00002"], units)
            self.assertFalse(result["complete"])

    def test_python_deduplicates_and_flags_inflation(self):
        task = {"id": "T1", "reported_count": 3, "reported_denominator": 3, "scope_text": "多数"}
        members = [{"player": "a", "count": True}, {"player": "a", "count": True}, {"player": "b", "count": False}, {"player": "c", "count": False}]
        result = aggregate_count(task, ["a", "b", "c"], members)
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["issues"]), 2)

    def test_missing_count_membership_not_passed(self):
        self.assertFalse(aggregate_count({"id": "T1"}, ["a", "b"], [{"player": "a", "count": True}])["complete"])

    def test_registry_missing_blocks_not_complete(self):
        report = "# 报告\n\n原句"
        obj = {"claims": [{"id": "C1", "original": "原句", "block_ids": ["B0002"]}], "coverage": [{"block_id": "B0002", "claim_ids": ["C1"]}]}
        self.assertFalse(validate_registry(obj, report, packet())["complete"])
        obj["coverage"].append({"block_id": "B0001", "no_claim": True})
        self.assertTrue(validate_registry(obj, report, packet())["complete"])

    def test_scope_unresolved_not_guessed(self):
        with self.assertRaises(ValueError):
            count_scope({"id": "T1", "question_keys": ["1"], "scope_unresolved": True}, packet())

    def test_registry_preserves_written_citations_and_multiblock_claim(self):
        report = "第一句 [q1/r2]\n\n第二句 [q2/r2]"
        obj = {"claims": [{"id": "C1", "original": report, "block_ids": ["B0001", "B0002"], "review_refs": []}],
               "coverage": [{"block_id": b, "claim_ids": ["C1"]} for b in ("B0001", "B0002")]}
        result = validate_registry(obj, report, packet())
        self.assertTrue(result["complete"])
        self.assertEqual(result["claims"][0]["review_refs"], ["q1/r2", "q2/r2"])
