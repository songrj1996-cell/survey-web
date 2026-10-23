"""services/export_service:报告导出到飞书云文档的业务编排。

把报告补好免责声明后上传为飞书文档(归登录用户),并经机器人通知;统一映射飞书权限错误。
"""
import re
import time

from fastapi import HTTPException

from app.core.runtime_logging import runtime_log
from app.integrations import feishu_client as feishu_export
from app.services.feishu_evidence_navigation import prepare_evidence_navigation
from app.services.feishu_navigation_auto_update import register_exported_navigation
from app.services.report_modes import prepare_report_markdown
from app.services.report_render import (
    _extract_feishu_callout_sections,
    _prep_export_md,
    _prep_feishu_export_md,
    _player_case_markdown,
)


def _clean_feishu_title(title: str | None) -> str:
    clean = re.sub(r"[\r\n\t]+", " ", str(title or "")).strip()
    clean = re.sub(r'[\\/:*?"<>|]', "_", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:120] or "调研报告"


def prepare_feishu_export_data(snapshot: dict, scope: str = "body") -> dict:
    """Read exact-version metadata; keep the source appendix outside case formatting."""
    body = prepare_report_markdown(snapshot, "body")
    full = body if scope == "body" else prepare_report_markdown(snapshot, scope)
    audit = snapshot.get("report_audit")
    plan = snapshot.get("plan") or {}
    return {
        "report_md": body,
        "evidence_md": full[len(body):],
        "player_cases": isinstance(audit, dict) and bool(audit),
        "mode": snapshot.get("mode") or plan.get("mode", ""),
        "title": snapshot.get("title") or None,
    }


async def _export_to_feishu(
    report_md: str, login: dict, mode: str = "", title: str | None = None,
    *, player_cases: bool = False, evidence_md: str = "",
) -> str:
    """将报告上传为飞书文档（docx），文档归登录用户所有，并通过机器人发消息通知。"""
    full = _prep_export_md(report_md, mode=mode)  # 补免责声明 + 去掉 CORE_START/END 标记
    title_m = re.search(r"^#\s+(.+?)$", full, re.MULTILINE)
    title = _clean_feishu_title(title or (title_m.group(1).strip() if title_m else "调研报告"))
    body = _prep_feishu_export_md(report_md, mode=mode)
    callout_sections = _extract_feishu_callout_sections(report_md)
    case_texts = []
    if player_cases:
        started = time.monotonic()
        try:
            formatted, texts = _player_case_markdown(body)
            sections = _extract_feishu_callout_sections(report_md, player_cases=True)
            body, case_texts, callout_sections = formatted, texts, sections
            runtime_log("飞书导出", "info", f"已准备 {len(texts)} 处典型玩家案例格式",
                        elapsed_seconds=round(time.monotonic() - started, 3))
        except Exception as exc:
            runtime_log("飞书导出", "warning", "典型玩家案例格式准备失败，已保留普通正文",
                        error_type=type(exc).__name__)
    # Keep legacy Feishu heading preparation, without applying case formatting
    # or injecting a second disclaimer into the source appendix.
    appendix = _prep_feishu_export_md(evidence_md, mode=mode, include_disclaimer=False) if evidence_md else ""
    # splitlines() in heading preparation consumes one terminal line break;
    # retain that break when it was internal to the original combined export.
    separator = "\n\n" + ("\n" if report_md.endswith(("\n", "\r")) else "")
    body, navigation = prepare_evidence_navigation(body + (separator + appendix if appendix else ""))
    open_id = login.get("open_id", "") or None
    url, doc_token, doc_type = await feishu_export.create_doc_via_bot(
        title,
        body,
        open_id,
        callout_sections=callout_sections,
        apply_report_format=True,
        **({"player_case_texts": case_texts} if case_texts else {}),
        **({"block_navigation": navigation} if navigation else {}),
    )
    if navigation and doc_type == "docx":
        await register_exported_navigation(doc_token, url, recipient_open_id=open_id or "", title=title)
    runtime_log(
        "飞书导出", "success", "飞书文档已经创建",
        title=title, url=url,
    )
    if open_id:
        await feishu_export.send_message_to_user(
            open_id,
            f"您的调研报告《{title}》已创建为飞书文档，点击查看：{url}"
        )
    return url


def _feishu_export_error(e: Exception) -> HTTPException:
    msg = str(e)
    if (
        "99991679" in msg
        or "drive:file:upload" in msg
        or "Unauthorized" in msg
        or "创建飞书云文档导入任务失败" in msg
        or "查询飞书云文档导入任务失败" in msg
    ):
        return HTTPException(
            status_code=403,
            detail=(
                "飞书授权缺少云文档上传/导入权限。请确认飞书开放平台已开通文件上传和云文档导入任务权限，"
                "然后点击左下角飞书账号退出登录，"
                "然后重新登录授权后再导出。"
            ),
        )
    return HTTPException(status_code=502, detail=f"创建飞书文档失败：{e}")
