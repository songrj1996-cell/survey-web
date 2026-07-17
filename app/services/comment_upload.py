"""services/comment_upload:评论分析上传业务——校验、文件保存、session 创建。"""
import hashlib
from pathlib import Path

from fastapi import HTTPException

from app.core.security import _assign_session_owner
from app.services.report_history import _find_comment_duplicate_report
from app.storage.sessions import _COMMENT_UPLOAD_DIR, get_session, new_session, save_session
from app.storage.settings import _load_app_settings


async def handle_comment_upload(
    filename: str,
    content: bytes,
    post_title: str,
    post_content: str,
    login: dict | None,
) -> dict:
    """校验上传文件、保存临时文件、创建 comment session，返回前端所需 result dict。"""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".csv", ".xlsx"}:
        raise HTTPException(status_code=400, detail="仅支持 CSV / Excel（.csv / .xlsx）文件")
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")
    post_title = (post_title or "").strip()
    post_content = (post_content or "").strip()
    if not post_title:
        raise HTTPException(status_code=400, detail="请填写帖子标题")
    if not post_content:
        raise HTTPException(status_code=400, detail="请填写帖子原文")

    file_hash = hashlib.sha256(content).hexdigest()
    duplicate_report = None
    if _load_app_settings().get("comment_duplicate_reminder_enabled", True):
        duplicate_report = _find_comment_duplicate_report(file_hash, login)

    sid = new_session()
    _COMMENT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_path = _COMMENT_UPLOAD_DIR / f"{sid}{suffix}"
    with open(upload_path, "wb") as f:
        f.write(content)

    print(
        f"[comment-upload] saved sid={sid} file={filename} size={len(content)} path={upload_path}",
        flush=True,
    )

    sess = get_session(sid)
    sess["kind"] = "comment"
    sess["mode"] = "comment"
    sess["filename"] = filename
    sess["comment_file_hash"] = file_hash
    sess["comment_upload_path"] = str(upload_path)
    sess["comment_upload_size"] = len(content)
    sess["comment_post_title"] = post_title
    sess["comment_post_content"] = post_content
    sess["comment_preprocess_done"] = False
    _assign_session_owner(sess, login)
    save_session(sid, sess)

    return {
        "session_id": sid,
        "filename": filename,
        "size": len(content),
        "preprocess_required": True,
        "duplicate_report": duplicate_report,
    }
