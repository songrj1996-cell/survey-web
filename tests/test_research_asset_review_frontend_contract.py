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
            "review.openForSnapshot(entry.snapshot_id, { trigger: reviewButton })",
            render,
        )
        module_ready = render.index("await loadAssetReviewModule()")
        capability_recheck = render.index(
            "if (!canReviewSnapshotAssets(entry))",
            module_ready,
        )
        open_review = render.index(
            "review.openForSnapshot(entry.snapshot_id, { trigger: reviewButton })",
            module_ready,
        )
        self.assertLess(module_ready, capability_recheck)
        self.assertLess(capability_recheck, open_review)
        self.assertIn("确认结果尚未保存", render)

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

    def test_review_routes_are_exact_same_origin_gets_only(self):
        route_templates = set(re.findall(
            r"`(/api/questionnaire-sources[^`]*)`",
            self.review_script,
        ))
        self.assertEqual(route_templates, {PROJECTION_ROUTE, THUMBNAIL_ROUTE})

        projection_endpoint = _function_body(
            self.review_script,
            "reviewEndpoint",
        )
        thumbnail_endpoint = _function_body(
            self.review_script,
            "thumbnailEndpoint",
        )
        for body in (projection_endpoint, thumbnail_endpoint):
            self.assertIn("sameOriginUrl(", body)
        self.assertIn("encodeURIComponent(snapshotId)", projection_endpoint)
        self.assertIn("encodeURIComponent(snapshotId)", thumbnail_endpoint)
        self.assertIn("encodeURIComponent(assetToken)", thumbnail_endpoint)

        methods = {
            method.upper()
            for method in re.findall(
                r"\bmethod\s*:\s*['\"]([A-Za-z]+)['\"]",
                self.review_script,
            )
        }
        self.assertEqual(methods, {"GET"})
        for name in ("loadProjection", "loadThumbnail"):
            with self.subTest(name=name):
                body = _function_body(self.review_script, name)
                self.assertRegex(body, r"\bfetch\s*\(")
                self.assertRegex(body, r"method\s*:\s*['\"]GET['\"]")
                self.assertRegex(body, r"credentials\s*:\s*['\"]same-origin['\"]")
                self.assertRegex(body, r"cache\s*:\s*['\"]no-store['\"]")
                self.assertRegex(body, r"redirect\s*:\s*['\"]error['\"]")

        for forbidden in (
            r"\bFormData\b",
            r"\bsendBeacon\b",
            r"\bmethod\s*:\s*['\"](?:POST|PUT|PATCH|DELETE)['\"]",
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
        self.assertIn("TOKEN_PATTERN.test(referenceToken)", normalize_item)
        self.assertIn("TOKEN_PATTERN.test(assetToken)", normalize_item)
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
        self.assertIn("exactInt(payload.total_references, 0, 2000)", normalize_projection)
        self.assertIn("payload.items.length > 2000", normalize_projection)
        self.assertIn("items.length !== totalReferences", normalize_projection)
        self.assertIn("reviewCount !== reviewRequiredReferences", normalize_projection)

    def test_projection_open_aborts_and_ignores_stale_or_closed_results(self):
        open_snapshot = _function_body(self.review_script, "openForSnapshot")
        self.assertIn("validateSnapshotId(snapshotId)", open_snapshot)
        self.assertIn("state.requestSerial += 1", open_snapshot)
        self.assertIn("const requestSerial = state.requestSerial", open_snapshot)
        self.assertIn("clearInFlightProjection()", open_snapshot)
        self.assertIn("new AbortController()", open_snapshot)
        self.assertIn("abortController.signal", open_snapshot)
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

        self.assertIn("const requestKey = Symbol(item.asset_token)", load_thumbnail)
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

    def test_copy_is_explicitly_preview_only_and_confirmation_is_unsaved(self):
        combined = self.catalog_script + "\n" + self.review_script
        self.assertIn("仅预览，确认结果尚未保存", self.review_script)
        self.assertIn("只读预览", combined)
        self.assertIn("确认结果尚未保存", combined)
        for misleading in (
            "确认结果已保存",
            "素材绑定已保存",
            "已自动确认素材",
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
