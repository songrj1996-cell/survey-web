from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import re
import unittest
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = PROJECT_ROOT / "static" / "index.html"
SCRIPT_PATH = (
    PROJECT_ROOT / "static" / "js" / "features"
    / "questionnaire-sources.js"
)
SURVEY_SCRIPT_PATH = (
    PROJECT_ROOT / "static" / "js" / "features"
    / "survey.js"
)
STYLESHEET_PATH = PROJECT_ROOT / "static" / "questionnaire-sources.css"

SCRIPT_URL = "/static/js/features/questionnaire-sources.js"
STYLESHEET_URL = "/static/questionnaire-sources.css"
SNAPSHOT_ANALYSIS_UPLOAD_PATH = (
    "/api/questionnaire-sources/snapshots/{snapshot_id}/analysis-sessions"
)

CAPABILITIES_URL = "/api/questionnaire-sources/capabilities"
SNAPSHOTS_URL = "/api/questionnaire-sources/snapshots"
POST_URLS = {
    SNAPSHOTS_URL,
    "/api/questionnaire-sources/bested/snapshots",
    "/api/questionnaire-sources/materials/snapshots",
    "/api/questionnaire-sources/materials/pdf/snapshots",
}
CAPABILITY_KEYS = {
    "snapshot_catalog",
    "snapshot_package_upload",
    "bested_original_questionnaire_upload",
    "screenshot_material_upload",
    "pdf_material_upload",
    "asset_review_projection",
}
OPTIONAL_CAPABILITY_KEYS = {
    "snapshot_analysis_session",
    "asset_review_decisions",
}

SOURCE_CONTRACTS = {
    "snapshot_package_upload": {
        "endpoint": SNAPSHOTS_URL,
        "field_name": "file",
        "accept": {".zip", "application/zip"},
        "multiple": False,
        "max_files": 1,
    },
    "bested_original_questionnaire_upload": {
        "endpoint": "/api/questionnaire-sources/bested/snapshots",
        "field_name": "file",
        "accept": {
            ".xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
        "multiple": False,
        "max_files": 1,
    },
    "screenshot_material_upload": {
        "endpoint": "/api/questionnaire-sources/materials/snapshots",
        "field_name": "files",
        "accept": {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            "image/png",
            "image/jpeg",
            "image/webp",
        },
        "multiple": True,
        "max_files": 20,
    },
    "pdf_material_upload": {
        "endpoint": "/api/questionnaire-sources/materials/pdf/snapshots",
        "field_name": "file",
        "accept": {".pdf", "application/pdf"},
        "multiple": False,
        "max_files": 1,
    },
}


class _IndexAssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.script_sources: list[str] = []
        self.stylesheet_hrefs: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "script" and attributes.get("src"):
            self.script_sources.append(attributes["src"] or "")
        if (
            tag == "link"
            and attributes.get("rel", "").casefold() == "stylesheet"
            and attributes.get("href")
        ):
            self.stylesheet_hrefs.append(attributes["href"] or "")


def _asset_path(url: str) -> str:
    return urlsplit(url).path


def _balanced_end(
    source: str,
    start: int,
    opening: str,
    closing: str,
) -> int:
    """Find a balanced JS/CSS delimiter while ignoring strings/comments."""
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

    raise AssertionError(f"unbalanced {opening!r} starting at offset {start}")


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


def _source_definition_blocks(source: str) -> dict[str, str]:
    declaration = re.search(r"\bconst\s+SOURCE_DEFS\s*=\s*\[", source)
    if declaration is None:
        raise AssertionError("missing SOURCE_DEFS array")
    array_start = source.find("[", declaration.start())
    array_end = _balanced_end(source, array_start, "[", "]")

    blocks: dict[str, str] = {}
    index = array_start + 1
    while index < array_end:
        if source[index] != "{":
            index += 1
            continue
        block_end = _balanced_end(source, index, "{", "}")
        block = source[index:block_end + 1]
        key = _string_property(block, "key")
        if key in blocks:
            raise AssertionError(f"duplicate SOURCE_DEFS key: {key}")
        blocks[key] = block
        index = block_end + 1
    return blocks


def _string_property(block: str, name: str) -> str:
    match = re.search(
        rf"\b{re.escape(name)}\s*:\s*(['\"])(.*?)\1",
        block,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing string property {name}")
    return match.group(2)


def _string_or_constant_property(source: str, block: str, name: str) -> str:
    match = re.search(
        rf"\b{re.escape(name)}\s*:\s*([A-Za-z_$][\w$]*|['\"].*?['\"])",
        block,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing string property {name}")
    token = match.group(1).strip()
    if token[:1] in {"'", '"'}:
        return token[1:-1]
    constant = re.search(
        rf"\bconst\s+{re.escape(token)}\s*=\s*(['\"])(.*?)\1\s*;",
        source,
        re.DOTALL,
    )
    if constant is None:
        raise AssertionError(f"{name} references non-string constant {token}")
    return constant.group(2)


def _boolean_property(block: str, name: str) -> bool:
    match = re.search(
        rf"\b{re.escape(name)}\s*:\s*(true|false)\b",
        block,
    )
    if match is None:
        raise AssertionError(f"missing boolean property {name}")
    return match.group(1) == "true"


def _integer_property(source: str, block: str, name: str) -> int:
    match = re.search(
        rf"\b{re.escape(name)}\s*:\s*([A-Za-z_$][\w$]*|\d+)\b",
        block,
    )
    if match is None:
        raise AssertionError(f"missing integer property {name}")
    token = match.group(1)
    if token.isdecimal():
        return int(token)
    constant = re.search(
        rf"\bconst\s+{re.escape(token)}\s*=\s*(\d+)\s*;",
        source,
    )
    if constant is None:
        raise AssertionError(f"{name} references non-numeric constant {token}")
    return int(constant.group(1))


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
                        f"unexpected CSS outside a rule: {region[index:].strip()!r}"
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


class QuestionnaireSourceFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index_html = INDEX_PATH.read_text(encoding="utf-8")
        cls.javascript = SCRIPT_PATH.read_text(encoding="utf-8")
        cls.survey_javascript = SURVEY_SCRIPT_PATH.read_text(encoding="utf-8")
        cls.stylesheet = STYLESHEET_PATH.read_text(encoding="utf-8")

    def test_index_loads_single_independent_script_in_safe_order(self):
        parser = _IndexAssetParser()
        parser.feed(self.index_html)
        script_paths = [_asset_path(source) for source in parser.script_sources]

        self.assertEqual(script_paths.count(SCRIPT_URL), 1)
        self.assertLess(
            script_paths.index("/static/js/core/core.js"),
            script_paths.index(SCRIPT_URL),
        )
        self.assertLess(
            script_paths.index(SCRIPT_URL),
            script_paths.index("/static/js/features/survey.js"),
        )
        self.assertEqual(
            script_paths.count("/static/js/features/interview.js"),
            1,
        )
        self.assertNotRegex(self.javascript, r"(?i)\binterview\b")

    def test_javascript_loads_the_independent_stylesheet_once(self):
        parser = _IndexAssetParser()
        parser.feed(self.index_html)
        self.assertNotIn(
            STYLESHEET_URL,
            [_asset_path(href) for href in parser.stylesheet_hrefs],
        )

        stylesheet_literals = re.findall(
            r"(['\"])(/static/questionnaire-sources\.css(?:\?[^'\"]*)?)\1",
            self.javascript,
        )
        self.assertEqual(
            [_asset_path(url) for _, url in stylesheet_literals],
            [STYLESHEET_URL],
        )

        ensure_stylesheet = _function_body(
            self.javascript,
            "ensureStylesheet",
        )
        self.assertRegex(
            ensure_stylesheet,
            r"document\.createElement\s*\(\s*['\"]link['\"]\s*\)",
        )
        self.assertRegex(
            ensure_stylesheet,
            r"\blink\.rel\s*=\s*['\"]stylesheet['\"]",
        )
        self.assertRegex(
            ensure_stylesheet,
            r"\blink\.href\s*=\s*['\"]"
            r"/static/questionnaire-sources\.css(?:\?[^'\"]*)?['\"]",
        )
        self.assertIn("document.head.appendChild(link)", ensure_stylesheet)

    def test_endpoint_whitelist_is_exact_and_uploads_are_post_only(self):
        route_literals = re.findall(
            r"(['\"])(/api/questionnaire-sources(?:/[^'\"]*)?)\1",
            self.javascript,
        )
        counts = Counter(route for _, route in route_literals)
        expected = {CAPABILITIES_URL, *POST_URLS}
        self.assertEqual(set(counts), expected)
        self.assertEqual(counts, Counter({route: 1 for route in expected}))

        self.assertRegex(
            self.javascript,
            r"fetch\s*\(\s*CAPABILITIES_URL\s*,\s*"
            r"\{\s*method\s*:\s*['\"]GET['\"]",
        )
        self.assertRegex(
            self.javascript,
            r"fetch\s*\(\s*snapshotCatalogUrl\s*\(",
        )
        self.assertRegex(
            self.javascript,
            r"fetch\s*\(\s*def\.endpoint\s*,\s*"
            r"\{\s*method\s*:\s*['\"]POST['\"]",
        )
        methods = {
            method.upper()
            for method in re.findall(
                r"\bmethod\s*:\s*['\"]([A-Za-z]+)['\"]",
                self.javascript,
            )
        }
        self.assertEqual(methods, {"GET", "POST"})

    def test_capability_keys_and_source_routes_have_one_to_one_mapping(self):
        capability_array = re.search(
            r"\bconst\s+CAPABILITY_KEYS\s*=\s*\[(.*?)\];",
            self.javascript,
            re.DOTALL,
        )
        self.assertIsNotNone(capability_array)
        keys = re.findall(r"['\"]([^'\"]+)['\"]", capability_array.group(1))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), CAPABILITY_KEYS)

        blocks = _source_definition_blocks(self.javascript)
        self.assertEqual(
            set(blocks),
            CAPABILITY_KEYS - {
                "snapshot_catalog",
                "asset_review_projection",
            },
        )
        self.assertEqual(
            {
                _string_or_constant_property(
                    self.javascript,
                    block,
                    "endpoint",
                )
                for block in blocks.values()
            },
            POST_URLS,
        )

    def test_asset_review_decision_capability_is_optional_and_boolean_safe(self):
        capability_array = re.search(
            r"\bconst\s+CAPABILITY_KEYS\s*=\s*\[(.*?)\];",
            self.javascript,
            re.DOTALL,
        )
        self.assertIsNotNone(capability_array)
        required_keys = set(re.findall(
            r"['\"]([^'\"]+)['\"]",
            capability_array.group(1),
        ))
        self.assertTrue(OPTIONAL_CAPABILITY_KEYS.isdisjoint(required_keys))

        normalize = _function_body(self.javascript, "normalizeCapabilities")
        self.assertIn(
            "normalized.asset_review_decisions = "
            "payload.asset_review_decisions === true",
            normalize,
        )
        self.assertIn(
            "normalized.snapshot_analysis_session = "
            "payload.snapshot_analysis_session === true",
            normalize,
        )
        self.assertNotRegex(
            normalize,
            r"typeof\s+payload\.asset_review_decisions\s*!==\s*"
            r"['\"]boolean['\"]",
        )

    def test_asset_review_launch_uses_latest_projection_and_write_capabilities(self):
        projection_gate = _function_body(
            self.javascript,
            "canReviewSnapshotAssets",
        )
        self.assertIn(
            "panelState.capabilities.asset_review_projection === true",
            projection_gate,
        )
        self.assertNotIn("asset_review_decisions", projection_gate)

        write_gate = _function_body(
            self.javascript,
            "canSubmitSnapshotAssetReviewDecisions",
        )
        self.assertIn(
            "panelState.capabilities.asset_review_decisions === true",
            write_gate,
        )
        self.assertNotIn("asset_review_projection", write_gate)

        render = _function_body(self.javascript, "renderCatalog")
        module_ready = render.index("await loadAssetReviewModule()")
        projection_recheck = render.index(
            "if (!canReviewSnapshotAssets(entry))",
            module_ready,
        )
        open_review = render.index(
            "review.openForSnapshot(entry.snapshot_id, {",
            projection_recheck,
        )
        latest_write_check = render.index(
            "writable: canSubmitSnapshotAssetReviewDecisions()",
            open_review,
        )
        self.assertLess(module_ready, projection_recheck)
        self.assertLess(projection_recheck, open_review)
        self.assertLess(open_review, latest_write_check)
        self.assertNotIn("writable: writable", render[open_review:])

        refresh = _function_body(self.javascript, "refresh")
        capability_assignment = refresh.index(
            "panelState.capabilities = result.capabilities",
        )
        revoke_check = refresh.index(
            "panelState.capabilities.asset_review_decisions !== true",
            capability_assignment,
        )
        close_review = refresh.index("closeAssetReview()", revoke_check)
        self.assertLess(capability_assignment, revoke_check)
        self.assertLess(revoke_check, close_review)

    def test_component_has_no_interview_storage_or_unsafe_global_patterns(self):
        forbidden = {
            "shared application state": r"(?<![\w$])state\s*(?:\.|\[|=)",
            "localStorage": r"\blocalStorage\b",
            "innerHTML": r"\binnerHTML\b",
            "outerHTML": r"\bouterHTML\b",
            "insertAdjacentHTML": r"\binsertAdjacentHTML\b",
            "document.write": r"\bdocument\s*\.\s*write\s*\(",
            "eval": r"\beval\s*\(",
            "Function constructor": r"\b(?:new\s+)?Function\s*\(",
            "global assignment": (
                r"\b(?:window|globalThis)\s*(?:\.\s*[A-Za-z_$][\w$]*|"
                r"\[\s*[^\]]+\])\s*=(?!=)"
            ),
        }
        for label, pattern in forbidden.items():
            with self.subTest(label=label):
                self.assertNotRegex(self.javascript, pattern)

        self.assertRegex(
            self.javascript,
            r"\A\s*['\"]use strict['\"]\s*;\s*\(\s*function\b",
        )
        self.assertRegex(self.javascript, r"\}\s*\)\s*\(\s*\)\s*;\s*\Z")

    def test_dynamic_content_is_rendered_as_text(self):
        element_helper = _function_body(self.javascript, "el")
        self.assertIn("document.createElement", element_helper)
        self.assertRegex(
            element_helper,
            r"\bnode\.textContent\s*=\s*text\b",
        )

        render_card = _function_body(self.javascript, "renderCard")
        self.assertRegex(
            render_card,
            r"\bfileName\.textContent\s*=\s*fileLabel\b",
        )
        self.assertRegex(
            render_card,
            r"\bfileMeta\.textContent\s*=",
        )
        set_message = _function_body(self.javascript, "setCardMessage")
        self.assertRegex(
            set_message,
            r"\bmessageNode\.textContent\s*=\s*['\"]['\"]",
        )
        self.assertIn("messageNode.appendChild", set_message)

    def test_error_responses_are_json_only_and_never_echo_raw_text(self):
        error_message = _function_body(
            self.javascript,
            "responseErrorMessage",
        )
        self.assertRegex(
            error_message,
            r"\bresponse\.json\s*\(\s*\)",
        )
        self.assertNotRegex(
            self.javascript,
            r"\bresponse\.text\s*\(",
        )
        self.assertNotRegex(
            error_message,
            r"\b(?:innerHTML|outerHTML|insertAdjacentHTML)\b",
        )

    def test_upload_and_capability_requests_abort_and_ignore_stale_results(self):
        upload = _function_body(self.javascript, "upload")
        self.assertRegex(
            upload,
            r"cardState\.requestSerial\s*\+=\s*1",
        )
        self.assertRegex(
            upload,
            r"const\s+requestSerial\s*=\s*cardState\.requestSerial",
        )
        self.assertIn("new AbortController()", upload)
        self.assertRegex(
            upload,
            r"signal\s*:\s*abortController\.signal",
        )
        self.assertGreaterEqual(
            len(re.findall(
                r"cardState\.requestSerial\s*!==\s*requestSerial",
                upload,
            )),
            3,
        )
        self.assertIn("abortController.signal.aborted", upload)

        refresh = _function_body(self.javascript, "refresh")
        self.assertRegex(
            refresh,
            r"capabilityRequestSerial\s*\+=\s*1",
        )
        self.assertRegex(
            refresh,
            r"const\s+requestSerial\s*=\s*"
            r"panelState\.capabilityRequestSerial",
        )
        self.assertIn("new AbortController()", refresh)
        self.assertIn(
            "fetchCapabilities(abortController.signal)",
            refresh,
        )
        self.assertRegex(
            refresh,
            r"panelState\.capabilityRequestSerial\s*!==\s*requestSerial",
        )
        self.assertIn("abortController.signal.aborted", refresh)

        refresh_catalog = _function_body(self.javascript, "refreshCatalog")
        self.assertRegex(
            refresh_catalog,
            r"catalogState\.requestSerial\s*\+=\s*1",
        )
        self.assertRegex(
            refresh_catalog,
            r"const\s+requestSerial\s*=\s*catalogState\.requestSerial",
        )
        self.assertIn("new AbortController()", refresh_catalog)
        self.assertIn("fetchCatalog(", refresh_catalog)
        self.assertRegex(
            refresh_catalog,
            r"catalogState\.requestSerial\s*!==\s*requestSerial",
        )
        self.assertIn("abortController.signal.aborted", refresh_catalog)

    def test_reset_aborts_in_flight_uploads_and_invalidates_serials(self):
        reset = _function_body(self.javascript, "reset")
        self.assertGreaterEqual(
            len(re.findall(
                r"cardState\.requestSerial\s*\+=\s*1",
                reset,
            )),
            2,
        )
        self.assertGreaterEqual(
            len(re.findall(
                r"cardState\.abortController\.abort\s*\(\s*\)",
                reset,
            )),
            2,
        )
        self.assertGreaterEqual(
            len(re.findall(
                r"requestSerial\s*:\s*cardState\.requestSerial",
                reset,
            )),
            2,
        )

    def test_invalid_or_failed_capability_probe_keeps_panel_hidden(self):
        mount_panel = _function_body(self.javascript, "mountPanel")
        self.assertRegex(mount_panel, r"\bpanel\.hidden\s*=\s*true\b")

        visibility = _function_body(self.javascript, "setPanelVisibility")
        self.assertRegex(
            visibility,
            r"\bpanel\.hidden\s*=\s*!\s*visible\b",
        )

        hidden_statuses = re.search(
            r"\bHTTP_HIDE_STATUSES\s*=\s*new\s+Set\s*\(\s*\[([^\]]*)\]",
            self.javascript,
        )
        self.assertIsNotNone(hidden_statuses)
        self.assertEqual(
            {int(value) for value in re.findall(r"\d+", hidden_statuses.group(1))},
            {401, 403, 404},
        )

        normalize = _function_body(self.javascript, "normalizeCapabilities")
        self.assertRegex(normalize, r"payload\.schema_version\s*!==\s*1")
        self.assertRegex(
            normalize,
            r"typeof\s+payload\s*\[\s*key\s*\]\s*!==\s*['\"]boolean['\"]",
        )

        fetch_capabilities = _function_body(
            self.javascript,
            "fetchCapabilities",
        )
        self.assertIn("HTTP_HIDE_STATUSES.has(response.status)", fetch_capabilities)
        self.assertGreaterEqual(
            len(re.findall(r"return\s*\{\s*hidden\s*:\s*true\s*\}", fetch_capabilities)),
            3,
        )

        refresh = _function_body(self.javascript, "refresh")
        self.assertRegex(
            refresh,
            r"result\.hidden\s*\|\|\s*!\s*result\.capabilities",
        )
        self.assertGreaterEqual(
            len(re.findall(r"setPanelVisibility\s*\(\s*false\s*\)", refresh)),
            2,
        )
        self.assertRegex(
            refresh,
            r"setPanelVisibility\s*\(\s*shouldShowPanel\(\)\s*\)",
        )
        self.assertIn("shouldShowPanel()", self.javascript)

    def test_file_accept_multiple_and_count_contracts_are_exact(self):
        blocks = _source_definition_blocks(self.javascript)
        for key, expected in SOURCE_CONTRACTS.items():
            with self.subTest(key=key):
                block = blocks[key]
                self.assertEqual(
                    _string_or_constant_property(
                        self.javascript,
                        block,
                        "endpoint",
                    ),
                    expected["endpoint"],
                )
                self.assertEqual(
                    _string_property(block, "fieldName"),
                    expected["field_name"],
                )
                accept = {
                    token.strip()
                    for token in _string_property(block, "accept").split(",")
                    if token.strip()
                }
                self.assertEqual(accept, expected["accept"])
                self.assertEqual(
                    _boolean_property(block, "multiple"),
                    expected["multiple"],
                )
                self.assertEqual(
                    _integer_property(self.javascript, block, "maxFiles"),
                    expected["max_files"],
                )

        for key in (
            "snapshot_package_upload",
            "bested_original_questionnaire_upload",
            "pdf_material_upload",
        ):
            self.assertRegex(
                blocks[key],
                r"files\.length\s*!==\s*this\.maxFiles",
            )
        screenshot_block = blocks["screenshot_material_upload"]
        self.assertRegex(screenshot_block, r"!\s*files\.length")
        self.assertRegex(
            screenshot_block,
            r"files\.length\s*>\s*this\.maxFiles",
        )

        create_card = _function_body(self.javascript, "createCard")
        self.assertRegex(create_card, r"input\.accept\s*=\s*def\.accept")
        self.assertRegex(
            create_card,
            r"input\.multiple\s*=\s*!!\s*def\.multiple",
        )
        upload = _function_body(self.javascript, "upload")
        self.assertLess(
            upload.index("def.validate(cardState.files)"),
            upload.index("fetch(def.endpoint"),
        )
        self.assertIn("formData.append(def.fieldName, file)", upload)

    def test_copy_is_honest_about_not_using_sources_in_current_report(self):
        self.assertIn(
            "这里保存的快照不会自动用于当前报告",
            self.javascript,
        )
        self.assertIn(
            "当前问卷分析流程不会自动改用这些快照",
            self.javascript,
        )
        self.assertIn(
            "已保存独立快照，不会自动用于当前报告",
            self.javascript,
        )
        for misleading in (
            "已用于当前报告",
            "已自动用于当前报告",
            "当前报告已引用",
        ):
            self.assertNotIn(misleading, self.javascript)

    def test_snapshot_analysis_selection_interface_is_minimal_and_frozen(self):
        self.assertIn("snapshot_analysis_session", self.javascript)
        self.assertIn(
            "Object.defineProperty(window, SNAPSHOT_ANALYSIS_INTERFACE_KEY",
            self.javascript,
        )
        self.assertIn("Object.freeze({", self.javascript)
        self.assertIn("getSelectedSnapshotId", self.javascript)
        self.assertIn("reset: () => resetAnalysisSelection()", self.javascript)
        for forbidden in ("owner", "raw", "media", "token"):
            self.assertNotRegex(
                self.javascript,
                rf"\b{forbidden}\b",
            )

    def test_catalog_selection_copy_requires_structure_and_excludes_images(self):
        self.assertIn("用于本次分析", self.javascript)
        self.assertIn("取消使用", self.javascript)
        self.assertIn("图片不会自动进入报告", self.javascript)
        self.assertRegex(
            self.javascript,
            r"Number\(entry\?\.(?:question_count|question_count)\s*\|\|\s*0\)\s*>\s*0",
        )
        self.assertIn("'aria-pressed'", self.javascript)

    def test_survey_upload_uses_snapshot_analysis_endpoint_when_selected(self):
        self.assertIn(
            "encodeURIComponent(normalized)",
            self.survey_javascript,
        )
        self.assertIn("/analysis-sessions", self.survey_javascript)
        self.assertIn("selectedSnapshotIdForAnalysis()", self.survey_javascript)
        self.assertIn("currentUploadEndpoint(snapshotId)", self.survey_javascript)
        self.assertIn(
            "if (!snapshotId) {\n    fd.append('source_type', sourceType);\n    if (questionnaireFile) fd.append('questionnaire_file', questionnaireFile);\n  }",
            self.survey_javascript,
        )
        self.assertIn("resetSelectedSnapshotAnalysis();", self.survey_javascript)
        self.assertIn("已选择问卷结构快照", self.survey_javascript)
        self.assertIn("图片不会自动进入报告", self.survey_javascript)

    def test_survey_upload_guards_duplicates_abort_and_safe_errors(self):
        self.assertIn("abortSurveyUpload();", self.survey_javascript)
        self.assertIn("new AbortController()", self.survey_javascript)
        self.assertIn("signal: abortController.signal", self.survey_javascript)
        self.assertGreaterEqual(
            len(re.findall(
                r"surveyUploadRequestSerial\s*!==\s*requestSerial",
                self.survey_javascript,
            )),
            3,
        )
        self.assertIn("responseDetailMessage(resp, fallback)", self.survey_javascript)
        self.assertIn("resp.status === 409", self.survey_javascript)
        self.assertIn("resp.status === 422", self.survey_javascript)

    def test_snapshot_catalog_contract_is_safe_and_paged(self):
        self.assertIn("snapshot_catalog", self.javascript)
        self.assertRegex(
            self.javascript,
            r"\bconst\s+SNAPSHOT_CATALOG_LIMIT\s*=\s*20\s*;",
        )
        self.assertRegex(
            self.javascript,
            r"fetch\s*\(\s*snapshotCatalogUrl\s*\(",
        )
        self.assertIn("encodeURIComponent(trimmedCursor)", self.javascript)
        self.assertIn("next_cursor", self.javascript)
        self.assertIn("catalogLoadMoreButton", self.javascript)
        self.assertIn("加载更多", self.javascript)
        self.assertIn(
            ".qsrc-catalog__load-more[hidden]",
            self.stylesheet,
        )
        self.assertNotRegex(
            self.javascript,
            r"\b(?:owner_ref|path|media|hash|raw_text|original_text)\b",
        )

    def test_upload_loading_copy_and_accessibility_contract(self):
        create_card = _function_body(self.javascript, "createCard")
        render_card = _function_body(self.javascript, "renderCard")
        upload = _function_body(self.javascript, "upload")
        self.assertIn("input.setAttribute('aria-label'", create_card)
        self.assertIn("取消上传", render_card)
        self.assertRegex(
            render_card,
            r"resetButton\.disabled\s*=\s*cardState\.phase\s*!==\s*['\"]loading['\"]\s*&&\s*!fileCount",
        )
        self.assertRegex(
            upload,
            r"HTTP_HIDE_STATUSES\.has\(response\.status\)",
        )
        self.assertIn("hidePanel('当前账号暂无本地问卷快照权限')", self.javascript)

    def test_stylesheet_rules_and_keyframes_are_qsrc_namespaced(self):
        self.assertTrue(STYLESHEET_PATH.is_file())
        self.assertTrue(self.stylesheet.strip())
        selectors, keyframes, _ = _parse_css_rules(self.stylesheet)
        self.assertTrue(selectors)
        for selector in selectors:
            with self.subTest(selector=selector):
                self.assertRegex(selector, r"\.qsrc-[A-Za-z0-9_-]+")
        for keyframe in keyframes:
            with self.subTest(keyframe=keyframe):
                self.assertTrue(keyframe.startswith("qsrc-"))

    def test_stylesheet_has_qsrc_scoped_responsive_rules(self):
        _, _, media_rules = _parse_css_rules(self.stylesheet)
        responsive = [
            (prelude, body)
            for prelude, body in media_rules
            if re.search(r"\b(?:max-width|min-width)\s*:", prelude)
        ]
        self.assertTrue(responsive)
        for prelude, body in responsive:
            with self.subTest(media=prelude):
                selectors, keyframes, nested_media = _parse_css_rules(body)
                self.assertTrue(selectors)
                self.assertFalse(keyframes)
                self.assertFalse(nested_media)
                self.assertTrue(
                    all(
                        re.search(r"\.qsrc-[A-Za-z0-9_-]+", selector)
                        for selector in selectors
                    )
                )


if __name__ == "__main__":
    unittest.main()
