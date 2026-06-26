"""services/export_service:报告导出到飞书云文档的业务编排。

把报告补好免责声明后上传为飞书文档(归登录用户),并经机器人通知;统一映射飞书权限错误。
"""
import re

from fastapi import HTTPException

from app.integrations import feishu_client as feishu_export
from app.services.report_render import _prep_export_md


async def _export_to_feishu(report_md: str, login: dict, mode: str = "") -> str:
    """将报告上传为飞书文档（docx），文档归登录用户所有，并通过机器人发消息通知。"""
    full = _prep_export_md(report_md, mode=mode)  # 补免责声明 + 去掉 CORE_START/END 标记
    title_m = re.search(r"^#\s+(.+?)$", full, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else "调研报告"
    open_id = login.get("open_id", "") or None
    url, _, _ = await feishu_export.create_doc_via_bot(title, full, open_id)
    print(f"[feishu-export] created doc title={title!r} url={url}")
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
