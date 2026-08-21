from __future__ import annotations

from pathlib import Path
import re
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = PROJECT_ROOT / "static" / "index.html"
CATALOG_SCRIPT_PATH = (
    PROJECT_ROOT / "static" / "js" / "features"
    / "questionnaire-sources.js"
)
REVIEW_SCRIPT_PATH = (
    PROJECT_ROOT / "static" / "js" / "features"
    / "research-asset-review.js"
)
STYLESHEET_PATH = PROJECT_ROOT / "static" / "research-asset-review.css"

REVIEW_SCRIPT_URL = "/static/js/features/research-asset-review.js"
STYLESHEET_URL = "/static/research-asset-review.css"
PROJECTION_ROUTE = (
    "/api/questionnaire-sources/snapshots/"
    "${encodeURIComponent(snapshotId)}/asset-review"
)
THUMBNAIL_ROUTE = (
    "/api/questionnaire-sources/snapshots/"
    "${encodeURIComponent(snapshotId)}/asset-review/thumbnails/"
    "${encodeURIComponent(assetToken)}.png"
)
DECISION_ROUTE = (
    "/api/questionnaire-sources/snapshots/"
    "${encodeURIComponent(snapshotId)}/asset-review/decisions"
)


def _balanced_end(
    source: str,
    start: int,
    opening: str,
    closing: str,
) -> int:
    """Find a balanced delimiter while ignoring JS/CSS strings and comments."""
    if start >= len(source) or source[start] != opening:
        raise AssertionError(f"expected {opening!r} at offset {start}")

    depth = 0
    index = start
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and following == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"`":
            quote = char
            index += 1
            continue
        if char == "/" and following == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and following == "*":
            block_comment = True
            index += 2
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise AssertionError(f"unbalanced {opening!r} at offset {start}")


def _function_body(source: str, name: str) -> str:
    declaration = re.search(
        rf"\b(?:async\s+)?function\s+{re.escape(name)}\s*\(",
        source,
    )
    if declaration is None:
        raise AssertionError(f"missing function {name}()")
    body_start = source.find("{", declaration.end())
    if body_start < 0:
        raise AssertionError(f"missing body for function {name}()")
    body_end = _balanced_end(source, body_start, "{", "}")
    return source[body_start + 1:body_end]


def _split_selector_list(selector_list: str) -> list[str]:
    selectors: list[str] = []
    start = 0
    depth = 0
    quote = ""
    escaped = False
    for index, char in enumerate(selector_list):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            selectors.append(selector_list[start:index].strip())
            start = index + 1
    selectors.append(selector_list[start:].strip())
    return [selector for selector in selectors if selector]


def _parse_css_rules(
    css: str,
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    clean = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    selectors: list[str] = []
    keyframes: list[str] = []
    media_rules: list[tuple[str, str]] = []

    def parse_region(region: str) -> None:
        index = 0
        while index < len(region):
            opening = region.find("{", index)
            if opening < 0:
                if region[index:].strip():
                    raise AssertionError(
                        "unexpected CSS outside a rule: "
                        f"{region[index:].strip()!r}"
                    )
                return
            prelude = region[index:opening].strip()
            closing = _balanced_end(region, opening, "{", "}")
            body = region[opening + 1:closing]
            if prelude.startswith("@media"):
                media_rules.append((prelude, body))
                parse_region(body)
            elif re.fullmatch(
                r"@(?:-[\w]+-)?keyframes\s+[\w-]+",
                prelude,
            ):
                keyframes.append(prelude.rsplit(maxsplit=1)[-1])
            elif prelude.startswith("@"):
                raise AssertionError(
                    f"unsupported global CSS at-rule: {prelude!r}"
                )
            else:
                selectors.extend(_split_selector_list(prelude))
            index = closing + 1

    parse_region(clean)
    return selectors, keyframes, media_rules


class ResearchAssetReviewFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index_html = INDEX_PATH.read_text(encoding="utf-8")
        cls.catalog_script = CATALOG_SCRIPT_PATH.read_text(encoding="utf-8")
        cls.review_script = REVIEW_SCRIPT_PATH.read_text(encoding="utf-8")
        cls.stylesheet = STYLESHEET_PATH.read_text(encoding="utf-8")

    def test_catalog_only_gates_and_launches_the_independent_module(self):
        gate = _function_body(self.catalog_script, "canReviewSnapshotAssets")
        self.assertIn("asset_review_projection === true", gate)
        self.assertRegex(
            gate,
            r"Number\(entry\?\.asset_reference_count\s*\|\|\s*0\)\s*>\s*0",
        )

        render = _function_body(self.catalog_script, "renderCatalog")
        self.assertIn("if (canReviewSnapshotAssets(entry))", render)
        self.assertIn("'查看素材'", render)
        self.assertIn("await loadAssetReviewModule()", render)
        self.assertIn(
            "review.openForSnapshot(entry.snapshot_id, {",
            render,
        )
        self.assertIn(
            "writable: canSubmitSnapshotAssetReviewDecisions()",
            render,
        )
        module_ready = render.index("await loadAssetReviewModule()")
        capability_recheck = render.index(
            "if (!canReviewSnapshotAssets(entry))",
            module_ready,
        )
        open_review = render.index(
            "review.openForSnapshot(entry.snapshot_id, {",
            module_ready,
        )
        self.assertLess(module_ready, capability_recheck)
        self.assertLess(capability_recheck, open_review)
        self.assertIn("当前账号不能提交确认", render)
        self.assertIn("可查看素材安全摘要并逐项确认", render)

        loader = _function_body(self.catalog_script, "loadAssetReviewModule")
        self.assertNotRegex(loader, r"\bfetch\s*\(")
        self.assertNotRegex(loader, r"\bXMLHttpRequest\b")
        self.assertIn("document.createElement('script')", loader)
        self.assertIn("document.head.appendChild(script)", loader)
        self.assertIn("assetReviewModulePromise = null", loader)
        self.assertIn("removeAssetReviewScriptNode()", loader)

        self.assertNotIn("/asset-review", self.catalog_script)
        for dto_field in (
            "asset_token",
            "reference_token",
            "warning_codes",
            "preview_status",
            "binding_confidence",
        ):
            with self.subTest(dto_field=dto_field):
                self.assertNotIn(dto_field, self.catalog_script)

    def test_script_and_stylesheet_are_loaded_dynamically_and_retryably(self):
        self.assertNotIn(REVIEW_SCRIPT_URL, self.index_html)
        self.assertNotIn(STYLESHEET_URL, self.index_html)
        self.assertRegex(
            self.catalog_script,
            r"['\"]/static/js/features/research-asset-review\.js"
            r"(?:\?[^'\"]*)?['\"]",
        )
        self.assertRegex(
            self.review_script,
            r"['\"]/static/research-asset-review\.css"
            r"(?:\?[^'\"]*)?['\"]",
        )

        ensure_stylesheet = _function_body(
            self.review_script,
            "ensureStylesheet",
        )
        self.assertIn("document.getElementById(STYLE_ID)", ensure_stylesheet)
        self.assertIn("document.createElement('link')", ensure_stylesheet)
        self.assertIn("link.rel = 'stylesheet'", ensure_stylesheet)
        self.assertIn("link.href = STYLE_URL", ensure_stylesheet)
        self.assertIn("document.head.appendChild(link)", ensure_stylesheet)

        remove_failed_script = _function_body(
            self.catalog_script,
            "removeAssetReviewScriptNode",
        )
        self.assertIn("removeChild(existing)", remove_failed_script)

    def test_review_routes_are_exact_same_origin_gets_and_one_post(self):
        route_templates = set(re.findall(
            r"`(/api/questionnaire-sources[^`]*)`",
            self.review_script,
        ))
        self.assertEqual(
            route_templates,
            {PROJECTION_ROUTE, THUMBNAIL_ROUTE, DECISION_ROUTE},
        )

        projection_endpoint = _function_body(
            self.review_script,
            "reviewEndpoint",
        )
        thumbnail_endpoint = _function_body(
            self.review_script,
            "thumbnailEndpoint",
        )
        decision_endpoint = _function_body(
            self.review_script,
            "decisionEndpoint",
        )
        for body in (
            projection_endpoint,
            thumbnail_endpoint,
            decision_endpoint,
        ):
            self.assertIn("sameOriginUrl(", body)
        self.assertIn("encodeURIComponent(snapshotId)", projection_endpoint)
        self.assertIn("encodeURIComponent(snapshotId)", thumbnail_endpoint)
        self.assertIn("encodeURIComponent(assetToken)", thumbnail_endpoint)
        self.assertIn("encodeURIComponent(snapshotId)", decision_endpoint)
        for secret in (
            "reference_token",
            "asset_token",
            "base_version_token",
            "idempotency_key",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, decision_endpoint)

        methods = {
            method.upper()
            for method in re.findall(
                r"\bmethod\s*:\s*['\"]([A-Za-z]+)['\"]",
                self.review_script,
            )
        }
        self.assertEqual(methods, {"GET", "POST"})
        self.assertEqual(
            len(re.findall(
                r"\bmethod\s*:\s*['\"]POST['\"]",
                self.review_script,
            )),
            1,
        )
        for name in ("loadProjection", "loadThumbnail"):
            with self.subTest(name=name):
                body = _function_body(self.review_script, name)
                self.assertRegex(body, r"\bfetch\s*\(")
                self.assertRegex(body, r"method\s*:\s*['\"]GET['\"]")
                self.assertRegex(body, r"credentials\s*:\s*['\"]same-origin['\"]")
                self.assertRegex(body, r"cache\s*:\s*['\"]no-store['\"]")
                self.assertRegex(body, r"redirect\s*:\s*['\"]error['\"]")

        post = _function_body(self.review_script, "postDecision")
        self.assertIn("fetch(decisionEndpoint(command.snapshotId)", post)
        self.assertRegex(post, r"method\s*:\s*['\"]POST['\"]")
        self.assertRegex(post, r"credentials\s*:\s*['\"]same-origin['\"]")
        self.assertRegex(post, r"cache\s*:\s*['\"]no-store['\"]")
        self.assertRegex(post, r"redirect\s*:\s*['\"]error['\"]")
        self.assertRegex(
            post,
            r"['\"]Content-Type['\"]\s*:\s*['\"]application/json['\"]",
        )
        self.assertIn("body: command.serialized_body", post)

        for forbidden in (
            r"\bFormData\b",
            r"\bsendBeacon\b",
            r"\bmethod\s*:\s*['\"](?:PUT|PATCH|DELETE)['\"]",
        ):
            self.assertNotRegex(self.review_script, forbidden)

    def test_endpoint_builder_rejects_cross_origin_urls(self):
        same_origin = _function_body(self.review_script, "sameOriginUrl")
        self.assertIn("new URL(path, window.location.origin)", same_origin)
        self.assertRegex(
            same_origin,
            r"url\.origin\s*!==\s*window\.location\.origin",
        )
        self.assertIn("throw new Error('素材审阅地址必须保持同源')", same_origin)

    def test_untrusted_values_use_dom_text_and_bounded_json_details(self):
        helper = _function_body(self.review_script, "el")
        self.assertIn("document.createElement(tag)", helper)
        self.assertIn("node.textContent = text", helper)

        render_status = _function_body(self.review_script, "renderStatus")
        self.assertGreaterEqual(render_status.count(".textContent ="), 5)
        render_items = _function_body(self.review_script, "renderItems")
        self.assertIn("document.createDocumentFragment()", render_items)
        self.assertIn("listNode.replaceChildren()", render_items)
        self.assertIn("fragment.appendChild(row)", render_items)

        error_detail = _function_body(self.review_script, "safeErrorDetail")
        self.assertRegex(error_detail, r"value\.trim\(\)\.length\s*<=\s*500")
        response_error = _function_body(
            self.review_script,
            "responseErrorMessage",
        )
        self.assertIn("response.json()", response_error)
        self.assertIn("safeErrorDetail(parsed.detail, fallback)", response_error)
        self.assertNotRegex(self.review_script, r"\bresponse\.text\s*\(")

    def test_module_has_no_html_injection_or_token_persistence_sink(self):
        combined = self.catalog_script + "\n" + self.review_script
        forbidden_patterns = {
            "innerHTML": r"\binnerHTML\b",
            "outerHTML": r"\bouterHTML\b",
            "insertAdjacentHTML": r"\binsertAdjacentHTML\b",
            "document.write": r"\bdocument\s*\.\s*write\s*\(",
            "eval": r"\beval\s*\(",
            "Function constructor": r"\b(?:new\s+)?Function\s*\(",
            "localStorage": r"\blocalStorage\b",
            "sessionStorage": r"\bsessionStorage\b",
            "IndexedDB": r"\bindexedDB\b",
            "cookie": r"\bdocument\.cookie\b",
            "URL query persistence": r"\bURLSearchParams\b",
            "history persistence": r"\bhistory\.(?:pushState|replaceState)\b",
            "token in dataset": r"\bdataset\.[A-Za-z_$][\w$]*(?:token|asset)",
            "token in attribute": (
                r"\bsetAttribute\s*\([^\n]*(?:asset_token|reference_token)"
            ),
            "token logging": r"\bconsole\.(?:log|info|warn|error)\s*\(",
        }
        for label, pattern in forbidden_patterns.items():
            with self.subTest(label=label):
                self.assertNotRegex(combined, pattern)

        api_start = self.review_script.index("const api = Object.freeze({")
        api_end = self.review_script.index("if (!Object.prototype", api_start)
        public_api = self.review_script[api_start:api_end]
        self.assertIn("openForSnapshot", public_api)
        self.assertIn("close()", public_api)
        for secret in ("asset_token", "reference_token", "projection", "state"):
            self.assertNotIn(secret, public_api)

        self.assertNotRegex(
            self.review_script,
            r"(?:textContent|innerText|setAttribute|dataset\.[A-Za-z_$][\w$]*)"
            r"[^\n]*(?:idempotency_key|base_version_token|reference_token)",
        )
        decision_endpoint = _function_body(
            self.review_script,
            "decisionEndpoint",
        )
        for secret in (
            "idempotency_key",
            "base_version_token",
            "reference_token",
            "asset_token",
        ):
            with self.subTest(decision_endpoint_secret=secret):
                self.assertNotIn(secret, decision_endpoint)

        catalog_render = _function_body(self.catalog_script, "renderCatalog")
        for secret in (
            "idempotency_key",
            "base_version_token",
            "reference_token",
            "asset_token",
        ):
            with self.subTest(catalog_secret=secret):
                self.assertNotIn(secret, catalog_render)

    def test_projection_validation_is_strict_and_bounded(self):
        validate_snapshot = _function_body(
            self.review_script,
            "validateSnapshotId",
        )
        self.assertIn("new TextEncoder().encode(normalized).length", validate_snapshot)
        self.assertIn("MAX_SNAPSHOT_ID_UTF8_BYTES", validate_snapshot)
        self.assertRegex(
            self.review_script,
            r"const\s+MAX_SNAPSHOT_ID_UTF8_BYTES\s*=\s*4096\s*;",
        )

        normalize_item = _function_body(
            self.review_script,
            "normalizeReviewItem",
        )
        self.assertRegex(
            normalize_item,
            r"Object\.prototype\.hasOwnProperty\.call\(\s*"
            r"raw,\s*['\"]active_review_decision['\"],?\s*\)",
        )
        self.assertRegex(
            normalize_item,
            r"(?:!\s*hasActiveReviewDecision|"
            r"!\s*Object\.prototype\.hasOwnProperty\.call\("
            r"raw,\s*['\"]active_review_decision['\"]\))",
        )
        self.assertIn("TOKEN_PATTERN.test(referenceToken)", normalize_item)
        self.assertIn("TOKEN_PATTERN.test(assetToken)", normalize_item)
        self.assertIn(
            "activeReviewDecision === 'confirmed' "
            "&& bindingStatus !== 'confirmed'",
            normalize_item,
        )
        self.assertIn(
            "activeReviewDecision === 'rejected' "
            "&& bindingStatus !== 'rejected'",
            normalize_item,
        )
        self.assertIn("raw.warning_codes.length > 64", normalize_item)
        self.assertIn("new Set(warningCodes).size !== warningCodes.length", normalize_item)
        self.assertIn("previewStatus === 'available' && mediaType !== 'image'", normalize_item)
        self.assertRegex(
            normalize_item,
            r"reviewRequired\s*!==\s*\(bindingStatus\s*===\s*['\"]proposed['\"]",
        )

        normalize_projection = _function_body(
            self.review_script,
            "normalizeProjection",
        )
        self.assertIn("exactInt(payload.schema_version, 1, 1)", normalize_projection)
        self.assertIn(
            "exactInt(payload.review_revision, 0, 10000)",
            normalize_projection,
        )
        self.assertIn(
            "boundedString(payload.base_version_token, 64, 64)",
            normalize_projection,
        )
        self.assertNotIn("writable ?", normalize_projection)
        self.assertNotIn("state.writable", normalize_projection)
        self.assertRegex(
            normalize_projection,
            r"reviewRevision\s*===\s*null",
        )
        self.assertRegex(
            normalize_projection,
            r"!\s*baseVersionToken",
        )
        self.assertIn("TOKEN_PATTERN.test(baseVersionToken)", normalize_projection)
        self.assertIn("exactInt(payload.total_references, 0, 2000)", normalize_projection)
        self.assertIn("payload.items.length > 2000", normalize_projection)
        self.assertIn("items.length !== totalReferences", normalize_projection)
        self.assertIn("reviewCount !== reviewRequiredReferences", normalize_projection)
        self.assertIn(
            "const referenceTokens = items.map(item => item.reference_token)",
            normalize_projection,
        )
        self.assertIn(
            "new Set(referenceTokens).size !== referenceTokens.length",
            normalize_projection,
        )

    def test_decision_command_is_webcrypto_backed_and_exactly_shaped(self):
        generate = _function_body(self.review_script, "generateHexToken")
        self.assertIn("new Uint8Array(32)", generate)
        self.assertIn("window.crypto.getRandomValues(bytes)", generate)
        self.assertIn("value.toString(16).padStart(2, '0')", generate)
        self.assertIn(".join('')", generate)
        self.assertRegex(
            self.review_script,
            r"const\s+TOKEN_PATTERN\s*=\s*/\^\[0-9a-f\]\{64\}\$/",
        )
        self.assertNotRegex(generate, r"\bMath\.random\b")

        decisions = re.search(
            r"\bconst\s+DECISIONS\s*=\s*new\s+Set\s*"
            r"\(\s*\[(.*?)\]\s*\)",
            self.review_script,
            re.DOTALL,
        )
        self.assertIsNotNone(decisions)
        self.assertEqual(
            set(re.findall(r"['\"]([^'\"]+)['\"]", decisions.group(1))),
            {"confirmed", "rejected", "reset"},
        )

        build = _function_body(self.review_script, "buildDecisionCommand")
        self.assertIn("schema_version: 1", build)
        self.assertIn("state.projection.review_revision", build)
        self.assertIn("idempotency_key: generateHexToken()", build)
        self.assertIn("state.projection.base_version_token", build)
        self.assertIn("item.reference_token", build)
        self.assertIn("item.asset_token", build)
        self.assertIn(
            "const serializedBody = JSON.stringify(commandPayload(command))",
            build,
        )
        self.assertIn("return Object.freeze({", build)
        self.assertIn("serialized_body: serializedBody", build)

        payload = _function_body(self.review_script, "commandPayload")
        body_fields = set(re.findall(
            r"\b([a-z_]+)\s*:\s*command\.\1\b",
            payload,
        ))
        self.assertEqual(
            body_fields,
            {
                "schema_version",
                "expected_revision",
                "idempotency_key",
                "base_version_token",
                "reference_token",
                "asset_token",
                "decision",
            },
        )
        self.assertNotIn("snapshotId:", payload)

    def test_decision_submit_is_single_flight_and_retry_is_manual_and_identical(self):
        submit = _function_body(self.review_script, "submitDecision")
        self.assertIn("state.writable !== true", submit)
        self.assertIn("if (!DECISIONS.has(decision)) return", submit)
        request_guard = submit.index("if (state.decisionRequest)")
        post_call = submit.index("await postDecision(command, controller.signal)")
        self.assertLess(request_guard, post_call)
        self.assertEqual(submit.count("postDecision("), 1)
        self.assertEqual(
            self.review_script.count(
                "await postDecision(command, controller.signal)",
            ),
            1,
        )

        retry_choice = "const command = sameDecisionTarget(pendingRetry, item, decision)"
        self.assertIn(retry_choice, submit)
        self.assertIn("? pendingRetry", submit)
        self.assertIn(": buildDecisionCommand(item, decision)", submit)
        self.assertIn("code === '504'", submit)
        self.assertIn("code === 'uncertain'", submit)
        self.assertIn("error instanceof TypeError", submit)
        self.assertIn("setUncertainDecisionCommand(command)", submit)
        self.assertIn("请重试同一操作确认最终状态", submit)
        retry_assignment = submit.index("setUncertainDecisionCommand(command)")
        self.assertNotIn("postDecision(", submit[retry_assignment:])

    def test_every_uncertain_write_result_preserves_the_frozen_serialized_command(self):
        build = _function_body(self.review_script, "buildDecisionCommand")
        self.assertIn("JSON.stringify(commandPayload(command))", build)
        self.assertIn("return Object.freeze({", build)
        self.assertIn("serialized_body: serializedBody", build)

        post = _function_body(self.review_script, "postDecision")
        self.assertIn("body: command.serialized_body", post)
        self.assertIn("response.status >= 500", post)
        self.assertIn(
            "error.code = response.status >= 500 ? 'uncertain'",
            post,
        )
        self.assertRegex(
            post,
            r"try\s*\{\s*payload\s*=\s*await\s+response\.json\(\)\s*;"
            r"\s*\}\s*catch\s*\{",
        )
        self.assertGreaterEqual(post.count("error.code = 'uncertain'"), 2)
        self.assertIn("if (!normalized)", post)

        submit = _function_body(self.review_script, "submitDecision")
        for uncertain in (
            "code === '504'",
            "code === 'uncertain'",
            "error instanceof TypeError",
        ):
            with self.subTest(uncertain=uncertain):
                self.assertIn(uncertain, submit)
        self.assertIn("setUncertainDecisionCommand(command)", submit)
        self.assertIn(
            "const pendingRetry = "
            "getUncertainDecisionCommand(state.snapshotId)",
            submit,
        )
        self.assertIn("? pendingRetry", submit)

    def test_uncertain_command_survives_close_switch_and_same_snapshot_reopen(self):
        self.assertIn(
            "uncertainDecisionCommands: Object.create(null)",
            self.review_script,
        )
        get_uncertain = _function_body(
            self.review_script,
            "getUncertainDecisionCommand",
        )
        self.assertIn("state.uncertainDecisionCommands[snapshotId]", get_uncertain)
        set_uncertain = _function_body(
            self.review_script,
            "setUncertainDecisionCommand",
        )
        self.assertIn(
            "state.uncertainDecisionCommands[command.snapshotId] = command",
            set_uncertain,
        )
        clear_uncertain = _function_body(
            self.review_script,
            "clearUncertainDecisionCommand",
        )
        self.assertIn(
            "delete state.uncertainDecisionCommands[snapshotId]",
            clear_uncertain,
        )

        clear_in_flight = _function_body(
            self.review_script,
            "clearInFlightDecision",
        )
        preserve = clear_in_flight.index(
            "setUncertainDecisionCommand(request.command)",
        )
        abort = clear_in_flight.index("request.controller.abort()", preserve)
        self.assertLess(preserve, abort)

        reset = _function_body(self.review_script, "resetProjectionState")
        self.assertNotIn("clearUncertainDecisionCommand", reset)
        self.assertNotIn("uncertainDecisionCommands =", reset)

        open_snapshot = _function_body(self.review_script, "openForSnapshot")
        self.assertIn("clearInFlightDecision()", open_snapshot)
        self.assertIn(
            "reconcileUncertainDecisionCommand(state.snapshotId, payload)",
            open_snapshot,
        )
        self.assertIn("上次审阅结果未确认，请原样重试同一操作", open_snapshot)

    def test_reopen_reconciles_uncertain_command_without_losing_retry_path(self):
        pair = _function_body(
            self.review_script,
            "projectionHasCommandPair",
        )
        self.assertIn(
            "item.reference_token === command.reference_token",
            pair,
        )
        self.assertIn("item.asset_token === command.asset_token", pair)

        reconcile = _function_body(
            self.review_script,
            "reconcileUncertainDecisionCommand",
        )
        self.assertIn("getUncertainDecisionCommand(snapshotId)", reconcile)
        base_change = reconcile.index(
            "projection.base_version_token "
            "!== pendingCommand.base_version_token",
        )
        clear = reconcile.index(
            "clearUncertainDecisionCommand(snapshotId)",
            base_change,
        )
        rebase = reconcile.index("phase: 'rebase'", clear)
        missing_pair = reconcile.index(
            "if (!projectionHasCommandPair(projection, pendingCommand))",
            rebase,
        )
        retry = reconcile.index("phase: 'retry'", missing_pair)
        self.assertLess(base_change, clear)
        self.assertLess(clear, rebase)
        self.assertLess(rebase, missing_pair)
        self.assertNotIn(
            "clearUncertainDecisionCommand",
            reconcile[missing_pair:retry],
        )
        self.assertIn("command: pendingCommand, phase: 'missing_pair'", reconcile)
        self.assertIn("command: pendingCommand, phase: 'retry'", reconcile)

        open_snapshot = _function_body(self.review_script, "openForSnapshot")
        reconcile_call = open_snapshot.index(
            "reconcileUncertainDecisionCommand(state.snapshotId, payload)",
        )
        missing_phase = open_snapshot.index(
            "reconciliation.phase === 'missing_pair'",
            reconcile_call,
        )
        projection_clear = open_snapshot.index(
            "state.projection = null",
            missing_phase,
        )
        fail_closed = open_snapshot.index(
            "setPhase('error'",
            projection_clear,
        )
        self.assertLess(reconcile_call, missing_phase)
        self.assertLess(missing_phase, projection_clear)
        self.assertLess(projection_clear, fail_closed)
        self.assertIn("已保留原重试命令", open_snapshot)
        self.assertIn("快照版本已变化，请重新审阅", open_snapshot)

        drawer = _function_body(self.review_script, "ensureDrawer")
        retry_handler = drawer[drawer.index("retryButton.addEventListener"):]
        self.assertIn("writable: state.writable", retry_handler)

    def test_existing_uncertain_retry_survives_deterministic_http_failures(self):
        submit = _function_body(self.review_script, "submitDecision")
        self.assertIn("const wasUncertainRetry = command === pendingRetry", submit)
        readonly = submit.index("if (code === 'readonly')")
        conflict = submit.index("if (code === '409')", readonly)
        self.assertIn(
            "setUncertainDecisionCommand(command)",
            submit[readonly:conflict],
        )
        deterministic = submit.index("} else {", conflict)
        finally_start = submit.index("} finally {", deterministic)
        deterministic_branch = submit[deterministic:finally_start]
        self.assertIn("if (wasUncertainRetry)", deterministic_branch)
        self.assertIn(
            "setUncertainDecisionCommand(command)",
            deterministic_branch,
        )
        self.assertIn("继续重试同一操作", deterministic_branch)
        self.assertIn(
            "clearUncertainDecisionCommand(command.snapshotId)",
            deterministic_branch,
        )

    def test_conflict_refresh_cannot_reinsert_the_sent_command(self):
        refresh = _function_body(self.review_script, "refreshAfterConflict")
        mark_unsent = refresh.index("state.decisionRequest.sent = false")
        clear = refresh.index(
            "clearUncertainDecisionCommand(snapshotId)",
            mark_unsent,
        )
        reopen = refresh.index("await openForSnapshot(snapshotId, {", clear)
        self.assertLess(mark_unsent, clear)
        self.assertLess(clear, reopen)
        self.assertIn("writable,", refresh)

        submit = _function_body(self.review_script, "submitDecision")
        conflict = submit.index("if (code === '409')")
        clear_in_submit = submit.index(
            "clearUncertainDecisionCommand(command.snapshotId)",
            conflict,
        )
        refresh_call = submit.index(
            "await refreshAfterConflict(focusState)",
            clear_in_submit,
        )
        self.assertLess(conflict, clear_in_submit)
        self.assertLess(clear_in_submit, refresh_call)

    def test_success_response_is_transaction_bound_before_state_replacement(self):
        validate = _function_body(
            self.review_script,
            "isSuccessfulDecisionProjection",
        )
        self.assertIn(
            "projection.base_version_token !== command.base_version_token",
            validate,
        )
        self.assertIn(
            "projection.review_revision < command.expected_revision + 1",
            validate,
        )
        self.assertIn(
            "item.reference_token === command.reference_token",
            validate,
        )
        self.assertIn(
            "item.asset_token === command.asset_token",
            validate,
        )

        submit = _function_body(self.review_script, "submitDecision")
        response = submit.index(
            "const projection = await postDecision(command, controller.signal)",
        )
        transaction_check = submit.index(
            "if (!isSuccessfulDecisionProjection(projection, command))",
            response,
        )
        uncertain_code = submit.index("error.code = 'uncertain'", transaction_check)
        clear_uncertain = submit.index(
            "clearUncertainDecisionCommand(command.snapshotId)",
            uncertain_code,
        )
        replace = submit.index("state.projection = projection", clear_uncertain)
        self.assertLess(response, transaction_check)
        self.assertLess(transaction_check, uncertain_code)
        self.assertLess(uncertain_code, clear_uncertain)
        self.assertLess(clear_uncertain, replace)

    def test_conflict_forces_get_refresh_without_replaying_the_post(self):
        submit = _function_body(self.review_script, "submitDecision")
        conflict = submit.index("if (code === '409')")
        clear_retry = submit.index(
            "clearUncertainDecisionCommand(command.snapshotId)",
            conflict,
        )
        refresh = submit.index("await refreshAfterConflict(focusState)", conflict)
        branch_return = submit.index("return;", refresh)
        self.assertLess(conflict, clear_retry)
        self.assertLess(clear_retry, refresh)
        self.assertLess(refresh, branch_return)
        self.assertNotIn("postDecision(", submit[conflict:branch_return])

        refresh_body = _function_body(
            self.review_script,
            "refreshAfterConflict",
        )
        self.assertIn("clearUncertainDecisionCommand(snapshotId)", refresh_body)
        self.assertIn("await openForSnapshot(snapshotId, {", refresh_body)
        self.assertIn("preserveNotice: true", refresh_body)
        self.assertIn("writable,", refresh_body)
        self.assertNotIn("postDecision(", refresh_body)
        open_snapshot = _function_body(self.review_script, "openForSnapshot")
        self.assertIn("loadProjection(normalized, abortController.signal)", open_snapshot)

    def test_conflict_refresh_preserves_notice_unless_safety_must_override(self):
        refresh = _function_body(self.review_script, "refreshAfterConflict")
        coordination_notice = refresh.index(
            "setNotice('另一页面已更新当前快照，请以最新结果为准', "
            "'conflict')",
        )
        reopen = refresh.index(
            "await openForSnapshot(snapshotId, {",
            coordination_notice,
        )
        preserve = refresh.index("preserveNotice: true", reopen)
        self.assertLess(coordination_notice, reopen)
        self.assertLess(reopen, preserve)
        self.assertNotIn("postDecision(", refresh)

        open_snapshot = _function_body(self.review_script, "openForSnapshot")
        get_projection = open_snapshot.index(
            "const payload = await loadProjection(normalized, "
            "abortController.signal)",
        )
        preserve_flag = open_snapshot.index(
            "const preserveNotice = !!(options && options.preserveNotice)",
            get_projection,
        )
        rebase = open_snapshot.index(
            "if (reconciliation.phase === 'rebase')",
            preserve_flag,
        )
        missing_pair = open_snapshot.index(
            "else if (reconciliation.phase === 'missing_pair')",
            rebase,
        )
        uncertain = open_snapshot.index(
            "else if (reconciliation.command)",
            missing_pair,
        )
        ordinary_review = open_snapshot.index(
            "else if (payload.review_required_references > 0 "
            "&& !(preserveNotice && state.notice))",
            uncertain,
        )
        self.assertLess(preserve_flag, rebase)
        self.assertLess(rebase, missing_pair)
        self.assertLess(missing_pair, uncertain)
        self.assertLess(uncertain, ordinary_review)

        rebase_branch = open_snapshot[rebase:missing_pair]
        missing_branch = open_snapshot[missing_pair:uncertain]
        uncertain_branch = open_snapshot[uncertain:ordinary_review]
        self.assertIn("快照版本已变化，请重新审阅", rebase_branch)
        self.assertIn("已保留原重试命令", missing_branch)
        self.assertIn("上次审阅结果未确认", uncertain_branch)
        for safety_branch in (
            rebase_branch,
            missing_branch,
            uncertain_branch,
        ):
            with self.subTest(safety_branch=safety_branch[:40]):
                self.assertNotIn("preserveNotice", safety_branch)

        ordinary_branch = open_snapshot[ordinary_review:]
        self.assertIn("结果以服务端返回为准", ordinary_branch)
        self.assertNotIn("postDecision(", open_snapshot)

    def test_projection_open_aborts_and_ignores_stale_or_closed_results(self):
        open_snapshot = _function_body(self.review_script, "openForSnapshot")
        self.assertIn("validateSnapshotId(snapshotId)", open_snapshot)
        self.assertIn("state.requestSerial += 1", open_snapshot)
        self.assertIn("const requestSerial = state.requestSerial", open_snapshot)
        self.assertIn("clearInFlightProjection()", open_snapshot)
        self.assertIn("new AbortController()", open_snapshot)
        self.assertIn("abortController.signal", open_snapshot)
        self.assertIn("clearInFlightDecision()", open_snapshot)
        self.assertIn("resetAllThumbnails()", open_snapshot)
        self.assertIn("state.writable = writable", open_snapshot)
        self.assertGreaterEqual(
            open_snapshot.count("state.requestSerial !== requestSerial"),
            2,
        )
        self.assertIn("!hasOpenDrawer()", open_snapshot)

        close = _function_body(self.review_script, "close")
        self.assertIn("state.requestSerial += 1", close)
        self.assertIn("clearInFlightProjection()", close)
        self.assertIn("resetProjectionState()", close)
        clear_projection = _function_body(
            self.review_script,
            "clearInFlightProjection",
        )
        self.assertIn("state.abortController.abort()", clear_projection)

        clear_decision = _function_body(
            self.review_script,
            "clearInFlightDecision",
        )
        self.assertIn("request.sent === true", clear_decision)
        self.assertIn("setUncertainDecisionCommand(request.command)", clear_decision)
        self.assertIn("request.controller.abort()", clear_decision)
        self.assertIn("state.decisionRequest = null", clear_decision)
        self.assertNotIn("uncertainDecisionCommands =", clear_decision)

        reset_projection = _function_body(
            self.review_script,
            "resetProjectionState",
        )
        for cleanup in (
            "clearInFlightDecision()",
            "state.projection = null",
            "state.snapshotId = ''",
            "state.writable = false",
            "clearDecisionViews()",
            "resetAllThumbnails()",
        ):
            with self.subTest(cleanup=cleanup):
                self.assertIn(cleanup, reset_projection)

    def test_success_replaces_projection_after_sensitive_command_cleanup(self):
        submit = _function_body(self.review_script, "submitDecision")
        success_start = submit.index(
            "const projection = await postDecision(command, controller.signal)",
        )
        stale_guard = submit.index(
            "state.requestSerial !== requestSerial",
            success_start,
        )
        clear_retry = submit.index(
            "clearUncertainDecisionCommand(command.snapshotId)",
            stale_guard,
        )
        replace_projection = submit.index("state.projection = projection", clear_retry)
        render = submit.index("render();", replace_projection)
        self.assertLess(stale_guard, clear_retry)
        self.assertLess(clear_retry, replace_projection)
        self.assertLess(replace_projection, render)
        finally_clear = submit.index("state.decisionRequest = null", render)
        self.assertLess(render, finally_clear)

    def test_late_decision_results_cannot_mutate_closed_or_switched_drawer(self):
        submit = _function_body(self.review_script, "submitDecision")
        self.assertIn("const requestSerial = state.requestSerial", submit)
        self.assertGreaterEqual(
            submit.count("state.requestSerial !== requestSerial"),
            2,
        )
        self.assertGreaterEqual(
            submit.count("controller.signal.aborted"),
            2,
        )
        self.assertGreaterEqual(submit.count("!hasOpenDrawer()"), 2)

        close = _function_body(self.review_script, "close")
        self.assertLess(
            close.index("state.requestSerial += 1"),
            close.index("resetProjectionState()"),
        )
        open_snapshot = _function_body(self.review_script, "openForSnapshot")
        clear_decision = open_snapshot.index(
            "clearInFlightDecision()",
        )
        snapshot_assignment = open_snapshot.index(
            "state.snapshotId = normalized",
        )
        self.assertLess(clear_decision, snapshot_assignment)

    def test_permission_loss_downgrades_to_read_only_and_preserves_retry(self):
        post = _function_body(self.review_script, "postDecision")
        self.assertIn("HIDE_STATUSES.has(response.status)", post)
        self.assertIn("hiddenError.code = 'readonly'", post)

        submit = _function_body(self.review_script, "submitDecision")
        readonly = submit.index("if (code === 'readonly')")
        conditional = submit.index("if (wasUncertainRetry)", readonly)
        preserve = submit.index(
            "setUncertainDecisionCommand(command)",
            conditional,
        )
        downgrade = submit.index("state.writable = false", preserve)
        rerender = submit.index("render();", downgrade)
        self.assertLess(readonly, conditional)
        self.assertLess(conditional, preserve)
        self.assertLess(preserve, downgrade)
        self.assertLess(downgrade, rerender)

    def test_decision_controls_are_accessible_and_restore_focus(self):
        actions = _function_body(self.review_script, "renderDecisionActions")
        self.assertIn("wrap.tabIndex = -1", actions)
        self.assertIn(
            "wrap.setAttribute('aria-label', "
            "`${item.context_label} 素材审阅操作`)",
            actions,
        )
        self.assertIn(
            "for (const decision of ['confirmed', 'rejected', 'reset'])",
            actions,
        )
        self.assertIn("button.disabled = disabled", actions)
        self.assertIn("button.setAttribute('aria-pressed'", actions)
        self.assertIn("button.setAttribute('aria-busy'", actions)
        self.assertIn("submitDecision(item, decision)", actions)

        drawer = _function_body(self.review_script, "ensureDrawer")
        self.assertIn("statusNode.setAttribute('aria-live', 'polite')", drawer)
        self.assertIn("noticeNode.setAttribute('aria-live', 'polite')", drawer)

        labels = _function_body(self.review_script, "decisionButtonLabel")
        for label in (
            "确认",
            "拒绝",
            "恢复待复核",
            "重试确认",
            "重试拒绝",
            "重试恢复待复核",
        ):
            with self.subTest(label=label):
                self.assertIn(label, labels)

        remember = _function_body(self.review_script, "rememberDecisionFocus")
        for action in ("confirmed", "rejected", "reset"):
            with self.subTest(action=action):
                self.assertIn(f"action: '{action}'", remember)

        restore = _function_body(self.review_script, "restoreDecisionFocus")
        self.assertIn("!target.disabled", restore)
        self.assertIn("target.focus()", restore)
        self.assertIn("document.activeElement === target", restore)
        self.assertIn("view.wrap.focus()", restore)

        submit = _function_body(self.review_script, "submitDecision")
        finally_start = submit.rindex("if (state.decisionRequest")
        clear_busy = submit.index("state.decisionRequest = null", finally_start)
        rerender = submit.index(
            "rerenderAllDecisionViews()",
            clear_busy,
        )
        focus = submit.index(
            "restoreDecisionFocus(focusState.referenceToken, focusState)",
            rerender,
        )
        self.assertLess(clear_busy, rerender)
        self.assertLess(rerender, focus)

        css_contracts = (
            '.qar-decision__button[aria-pressed="true"]',
            '.qar-decision__button[aria-busy="true"]',
            ".qar-decision__status.is-busy",
            ".qar-decision__status.is-conflict",
            ".qar-decision__status.is-readonly",
        )
        for selector in css_contracts:
            with self.subTest(selector=selector):
                self.assertIn(selector, self.stylesheet)

    def test_decision_rerender_cannot_append_into_the_views_it_iterates(self):
        rerender = _function_body(
            self.review_script,
            "rerenderDecisionViews",
        )
        snapshot = rerender.index(
            "const views = getDecisionViews(referenceToken).filter(",
        )
        loop = rerender.index("for (const view of views)", snapshot)
        render_without_registration = rerender.index(
            "renderDecisionActions(view.item, { registerView: false })",
            loop,
        )
        collect_replacement = rerender.index(
            "nextViews.push({",
            render_without_registration,
        )
        publish_replacements = rerender.index(
            "state.decisionViews[referenceToken] = nextViews",
            collect_replacement,
        )
        self.assertLess(snapshot, loop)
        self.assertLess(loop, render_without_registration)
        self.assertLess(render_without_registration, collect_replacement)
        self.assertLess(collect_replacement, publish_replacements)
        self.assertNotIn("views.push(", rerender)
        self.assertNotIn("registerDecisionView(", rerender)

        render_actions = _function_body(
            self.review_script,
            "renderDecisionActions",
        )
        registration_gate = render_actions.index(
            "if (settings.registerView !== false)",
        )
        registration = render_actions.index(
            "registerDecisionView(item.reference_token",
            registration_gate,
        )
        self.assertLess(registration_gate, registration)

        render_items = _function_body(self.review_script, "renderItems")
        self.assertIn("renderDecisionActions(item)", render_items)
        self.assertNotIn("registerView: false", render_items)

    def test_single_write_gate_updates_every_decision_row_from_a_bounded_snapshot(self):
        render_actions = _function_body(
            self.review_script,
            "renderDecisionActions",
        )
        self.assertIn("const busy = !!state.decisionRequest", render_actions)
        self.assertIn(
            "const disabled = !state.writable || !state.projection || busy",
            render_actions,
        )
        self.assertIn("button.disabled = disabled", render_actions)
        self.assertIn("button.setAttribute('aria-busy', busy", render_actions)

        rerender_all = _function_body(
            self.review_script,
            "rerenderAllDecisionViews",
        )
        snapshot = rerender_all.index(
            "const referenceTokens = Object.keys(state.decisionViews)",
        )
        loop = rerender_all.index(
            "for (const referenceToken of referenceTokens)",
            snapshot,
        )
        rerender_one = rerender_all.index(
            "rerenderDecisionViews(referenceToken)",
            loop,
        )
        self.assertLess(snapshot, loop)
        self.assertLess(loop, rerender_one)
        self.assertNotIn("for (const referenceToken in state.decisionViews)", rerender_all)
        self.assertNotIn("renderDecisionActions(", rerender_all)
        self.assertNotIn("registerDecisionView(", rerender_all)
        self.assertNotRegex(
            rerender_all,
            r"state\.decisionViews\s*(?:=|\[[^\]]+\]\s*=)",
        )

        submit = _function_body(self.review_script, "submitDecision")
        request_start = submit.index("state.decisionRequest = {")
        publish_busy = submit.index("rerenderAllDecisionViews()", request_start)
        post = submit.index(
            "await postDecision(command, controller.signal)",
            publish_busy,
        )
        self.assertLess(request_start, publish_busy)
        self.assertLess(publish_busy, post)

        catch = submit.index("} catch (error) {")
        catch_rerender = submit.index("rerenderAllDecisionViews()", catch)
        finally_block = submit.index("} finally {", catch_rerender)
        clear_busy = submit.index(
            "state.decisionRequest = null",
            finally_block,
        )
        release_all_rows = submit.index(
            "rerenderAllDecisionViews()",
            clear_busy,
        )
        restore_target = submit.index(
            "restoreDecisionFocus(focusState.referenceToken, focusState)",
            release_all_rows,
        )
        self.assertLess(catch, catch_rerender)
        self.assertLess(catch_rerender, finally_block)
        self.assertLess(finally_block, clear_busy)
        self.assertLess(clear_busy, release_all_rows)
        self.assertLess(release_all_rows, restore_target)

    def test_thumbnail_is_manual_lazy_get_and_releases_object_urls(self):
        shell = _function_body(self.review_script, "renderThumbnailShell")
        self.assertIn("'加载缩略图'", shell)
        self.assertIn("手动加载后才会请求缩略图", shell)
        self.assertIn("button.addEventListener('click', () => loadThumbnail(item))", shell)
        self.assertNotRegex(shell, r"\bfetch\s*\(")
        self.assertNotIn("thumbnailEndpoint(", shell)

        load_thumbnail = _function_body(self.review_script, "loadThumbnail")
        self.assertIn("fetch(thumbnailEndpoint(state.snapshotId, item.asset_token)", load_thumbnail)
        self.assertIn("response.blob()", load_thumbnail)
        self.assertIn("contentType !== 'image/png'", load_thumbnail)
        self.assertIn("MAX_THUMBNAIL_BYTES", load_thumbnail)
        self.assertIn("URL.createObjectURL(blob)", load_thumbnail)
        self.assertIn("URL.revokeObjectURL(objectUrl)", load_thumbnail)

        reset_thumbnail = _function_body(
            self.review_script,
            "resetThumbnailState",
        )
        self.assertIn("entry.controller.abort()", reset_thumbnail)
        self.assertIn("releaseThumbnailObject(entry)", reset_thumbnail)
        release_thumbnail = _function_body(
            self.review_script,
            "releaseThumbnailObject",
        )
        self.assertIn("URL.revokeObjectURL(entry.objectUrl)", release_thumbnail)
        self.assertIn("resetAllThumbnails()", _function_body(
            self.review_script,
            "resetProjectionState",
        ))

    def test_thumbnail_gate_is_two_fail_fast_and_identity_safe_across_reopen(self):
        self.assertRegex(
            self.review_script,
            r"const\s+MAX_CONCURRENT_THUMBNAILS\s*=\s*2\s*;",
        )
        load_thumbnail = _function_body(self.review_script, "loadThumbnail")
        gate = "state.activeThumbnailRequests.size >= MAX_CONCURRENT_THUMBNAILS"
        fetch_call = "fetch(thumbnailEndpoint(state.snapshotId, item.asset_token)"
        self.assertIn(gate, load_thumbnail)
        self.assertLess(load_thumbnail.index(gate), load_thumbnail.index(fetch_call))
        self.assertIn("请稍后重试", load_thumbnail)
        gate_position = load_thumbnail.index(gate)
        gate_return = load_thumbnail.index("return;", gate_position)
        self.assertLess(gate_return, load_thumbnail.index(fetch_call))

        self.assertIn("const requestKey = Symbol()", load_thumbnail)
        self.assertNotIn("Symbol(item.asset_token)", load_thumbnail)
        self.assertIn("state.activeThumbnailRequests.add(requestKey)", load_thumbnail)
        self.assertIn("state.activeThumbnailRequests.delete(requestKey)", load_thumbnail)
        self.assertIn("thumbState.controller === controller", load_thumbnail)
        self.assertNotIn("activeThumbnailRequests.clear", self.review_script)

        reset_all = _function_body(self.review_script, "resetAllThumbnails")
        close = _function_body(self.review_script, "close")
        self.assertNotIn("activeThumbnailRequests", reset_all)
        self.assertNotIn("activeThumbnailRequests", close)
        self.assertIn("resetThumbnailState(entry)", reset_all)

    def test_thumbnail_resident_cache_is_count_and_byte_bounded(self):
        self.assertRegex(
            self.review_script,
            r"const\s+MAX_CACHED_THUMBNAILS\s*=\s*24\s*;",
        )
        self.assertRegex(
            self.review_script,
            r"const\s+MAX_CACHED_THUMBNAIL_BYTES\s*=\s*"
            r"64\s*\*\s*1024\s*\*\s*1024\s*;",
        )

        thumbnail_state = _function_body(
            self.review_script,
            "getThumbnailState",
        )
        self.assertIn("assetToken,", thumbnail_state)
        self.assertIn("objectSize: 0", thumbnail_state)

        load_thumbnail = _function_body(self.review_script, "loadThumbnail")
        success_steps = (
            "thumbState.objectUrl = objectUrl",
            "thumbState.objectSize = blob.size",
            "touchThumbnailLru(item.asset_token)",
            "state.cachedThumbnailBytes += blob.size",
            "evictThumbnailCache(item.asset_token)",
        )
        success_start = load_thumbnail.index(success_steps[0])
        positions = [
            load_thumbnail.index(step, success_start)
            for step in success_steps
        ]
        self.assertEqual(positions, sorted(positions))

        touch = _function_body(self.review_script, "touchThumbnailLru")
        self.assertIn(
            "state.thumbnailLru.filter(token => token !== assetToken)",
            touch,
        )
        self.assertIn("state.thumbnailLru.push(assetToken)", touch)

        evict = _function_body(self.review_script, "evictThumbnailCache")
        self.assertIn(
            "state.thumbnailLru.length > MAX_CACHED_THUMBNAILS",
            evict,
        )
        self.assertIn(
            "state.cachedThumbnailBytes > MAX_CACHED_THUMBNAIL_BYTES",
            evict,
        )
        self.assertIn("releaseThumbnailObject(victimState)", evict)
        self.assertIn("victimState.phase = 'idle'", evict)
        self.assertIn("victimState.error = ''", evict)
        self.assertIn("rerenderThumbnailViews(victimAssetToken)", evict)

        release = _function_body(
            self.review_script,
            "releaseThumbnailObject",
        )
        self.assertIn("URL.revokeObjectURL(entry.objectUrl)", release)
        self.assertIn(
            "state.cachedThumbnailBytes - entry.objectSize",
            release,
        )
        self.assertIn("entry.objectUrl = ''", release)
        self.assertIn("entry.objectSize = 0", release)

        reset_all = _function_body(self.review_script, "resetAllThumbnails")
        self.assertIn("state.cachedThumbnailBytes = 0", reset_all)
        self.assertIn("state.thumbnailLru = []", reset_all)

    def test_duplicate_asset_references_share_one_request_and_update_local_shells(self):
        thumbnail_state = _function_body(
            self.review_script,
            "getThumbnailState",
        )
        self.assertIn("state.thumbnailStates[assetToken]", thumbnail_state)
        render_shell = _function_body(
            self.review_script,
            "renderThumbnailShell",
        )
        self.assertIn("getThumbnailState(item.asset_token)", render_shell)

        load_thumbnail = _function_body(self.review_script, "loadThumbnail")
        self.assertIn("if (thumbState.phase === 'loading')", load_thumbnail)
        self.assertIn("if (thumbState.phase === 'ready' && thumbState.objectUrl)", load_thumbnail)
        first_fetch = load_thumbnail.index("fetch(thumbnailEndpoint(")
        self.assertLess(load_thumbnail.index("thumbState.phase === 'loading'"), first_fetch)
        self.assertLess(load_thumbnail.index("thumbState.phase === 'ready'"), first_fetch)

        register_view = _function_body(
            self.review_script,
            "registerThumbnailView",
        )
        self.assertIn("getThumbnailViews(assetToken)", register_view)
        self.assertIn("views.push({ shell, item })", register_view)
        rerender = _function_body(
            self.review_script,
            "rerenderThumbnailViews",
        )
        self.assertIn("renderThumbnailShell(view.item)", rerender)
        self.assertIn("currentShell.replaceWith(nextShell)", rerender)
        self.assertIn("nextViews.push({ shell: nextShell, item: view.item })", rerender)
        self.assertNotIn("projection.items.find", self.review_script)
        self.assertNotIn("renderItems()", load_thumbnail)

    def test_drawer_closes_with_escape_traps_focus_and_restores_focus(self):
        ensure_drawer = _function_body(self.review_script, "ensureDrawer")
        self.assertIn("panel.setAttribute('role', 'dialog')", ensure_drawer)
        self.assertIn("panel.setAttribute('aria-modal', 'true')", ensure_drawer)
        self.assertIn("document.addEventListener('keydown', handleGlobalKeydown)", ensure_drawer)

        keydown = _function_body(self.review_script, "handleGlobalKeydown")
        self.assertIn("event.key === 'Escape'", keydown)
        self.assertIn("close()", keydown)
        self.assertIn("event.key !== 'Tab'", keydown)
        self.assertIn("panel.contains(active)", keydown)
        self.assertIn("last.focus()", keydown)
        self.assertIn("first.focus()", keydown)

        open_drawer = _function_body(self.review_script, "openDrawer")
        self.assertIn("state.restoreFocusTarget = trigger", open_drawer)
        self.assertIn("requestAnimationFrame", open_drawer)
        self.assertIn("state.openSequence === openSequence", open_drawer)

        close = _function_body(self.review_script, "close")
        self.assertIn("document.contains(target)", close)
        self.assertIn("target.focus()", close)
        self.assertIn("state.openSequence += 1", close)

        shell = _function_body(self.review_script, "renderThumbnailShell")
        self.assertIn("shell.tabIndex = -1", shell)
        self.assertIn("shell.setAttribute('aria-live', 'polite')", shell)
        rerender = _function_body(
            self.review_script,
            "rerenderThumbnailViews",
        )
        self.assertIn("rememberThumbnailFocus(views)", rerender)
        self.assertIn("restoreThumbnailFocus(assetToken, focusState)", rerender)

        restore_thumbnail = _function_body(
            self.review_script,
            "restoreThumbnailFocus",
        )
        self.assertIn(
            "shell.querySelector('.qar-thumb__button:not([disabled])')",
            restore_thumbnail,
        )
        self.assertIn("button.focus()", restore_thumbnail)
        self.assertIn("document.activeElement === button", restore_thumbnail)
        self.assertIn("shell.focus()", restore_thumbnail)
        self.assertLess(
            restore_thumbnail.index("button.focus()"),
            restore_thumbnail.index("shell.focus()"),
        )

    def test_all_safe_http_statuses_have_human_readable_copy(self):
        hide_statuses = re.search(
            r"\bHIDE_STATUSES\s*=\s*new\s+Set\s*\(\s*\[([^\]]*)\]",
            self.review_script,
        )
        self.assertIsNotNone(hide_statuses)
        self.assertEqual(
            {int(value) for value in re.findall(r"\d+", hide_statuses.group(1))},
            {401, 403},
        )
        self.assertIn("当前账号暂无查看素材预览的权限", self.review_script)
        self.assertIn("当前账号暂无缩略图预览权限", self.review_script)

        for status in (404, 422, 429, 500, 504):
            with self.subTest(status=status):
                self.assertGreaterEqual(
                    self.review_script.count(f"response.status === {status}"),
                    2,
                )
        for message in (
            "问卷素材审阅内容不存在",
            "素材预览被安全策略阻止",
            "当前素材预览入口繁忙，请稍后再试",
            "素材预览处理超时，请稍后重试",
            "素材审阅暂时不可用",
            "缩略图不存在",
            "缩略图被安全策略阻止",
            "缩略图入口繁忙，请稍后再试",
            "缩略图处理超时，请稍后重试",
            "缩略图暂时不可用",
        ):
            with self.subTest(message=message):
                self.assertIn(message, self.review_script)

    def test_copy_distinguishes_writable_and_read_only_review_modes(self):
        combined = self.catalog_script + "\n" + self.review_script
        self.assertIn("可逐项确认、拒绝或恢复待复核", self.review_script)
        self.assertIn("当前账号不能提交确认", combined)
        self.assertIn("只读预览", combined)
        self.assertIn("结果以服务端返回为准", self.review_script)
        self.assertIn("素材决定已保存并同步到最新快照", self.review_script)
        for misleading in (
            "已自动确认素材",
            "无需人工复核",
            "本地已保存",
        ):
            self.assertNotIn(misleading, combined)

    def test_styles_are_qar_namespaced_and_responsive(self):
        self.assertTrue(self.stylesheet.strip())
        selectors, keyframes, media_rules = _parse_css_rules(self.stylesheet)
        self.assertTrue(selectors)
        for selector in selectors:
            with self.subTest(selector=selector):
                self.assertRegex(selector, r"\.qar-[A-Za-z0-9_-]+")
        for keyframe in keyframes:
            with self.subTest(keyframe=keyframe):
                self.assertTrue(keyframe.startswith("qar-"))

        responsive = [
            (prelude, body)
            for prelude, body in media_rules
            if re.search(r"\b(?:max-width|min-width)\s*:", prelude)
        ]
        self.assertTrue(responsive)
        for prelude, body in responsive:
            with self.subTest(media=prelude):
                nested_selectors, _, nested_media = _parse_css_rules(body)
                self.assertTrue(nested_selectors)
                self.assertFalse(nested_media)
                self.assertTrue(all(
                    re.search(r"\.qar-[A-Za-z0-9_-]+", selector)
                    for selector in nested_selectors
                ))

    def test_does_not_couple_to_interview_v1_or_v2_frontend_state(self):
        combined = self.catalog_script + "\n" + self.review_script
        for forbidden in (
            "ivState",
            "interviewV2Feature",
            "data-iv-",
            "/api/interview",
            "/static/js/features/interview.js",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, combined)
        self.assertNotRegex(self.stylesheet, r"\.iv-[A-Za-z0-9_-]+")


if __name__ == "__main__":
    unittest.main()
