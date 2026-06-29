"""storage/sessions:分析会话(data/sessions/<uuid>.json)读写 + 过期清理。

注:_session_path / get_session 沿用原实现,在 sid 非法或会话不存在时抛 HTTPException
——这是 storage 触及 HTTP 的已知例外(本轮保持行为不变,不改),后续可改为抛 ValueError
由 router 负责转换。
"""
import json
import os
import re
import time
import uuid
from pathlib import Path

from fastapi import HTTPException

from app.core.config import DATA_DIR

_SESSION_DIR = Path("data") / "sessions"
_COMMENT_UPLOAD_DIR = Path(DATA_DIR) / "comment_uploads"
SESSION_TTL = 7200  # 2 小时，用于 sweep


def _session_path(sid: str) -> Path:
    # 校验 sid 格式，防止路径穿越
    if not re.match(r"^[0-9a-f\-]{32,36}$", sid):
        raise HTTPException(status_code=400, detail="无效的会话 ID")
    return _SESSION_DIR / f"{sid}.json"


def _sweep_old_sessions() -> None:
    """启动时清理过期文件，避免 data/sessions/ 无限增长。"""
    if not _SESSION_DIR.exists():
        return
    cutoff = time.time() - SESSION_TTL
    for p in _SESSION_DIR.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        except Exception:
            pass
    if _COMMENT_UPLOAD_DIR.exists():
        for p in _COMMENT_UPLOAD_DIR.glob("*"):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except Exception:
                pass


def new_session() -> str:
    _SESSION_DIR.mkdir(parents=True, exist_ok=True)
    sid = str(uuid.uuid4())
    _write_session(sid, {"ts": time.time()})
    return sid


def get_session(sid: str) -> dict:
    p = _session_path(sid)
    if not p.exists():
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新上传文件")
    with open(p, "r", encoding="utf-8") as f:
        sess = json.load(f)
    # JSON 不支持整数 key，恢复 open_text / crosstab_questions 的 int key
    for field in ("open_text", "crosstab_questions"):
        if isinstance(sess.get(field), dict):
            sess[field] = {int(k): v for k, v in sess[field].items()}
    sess["ts"] = time.time()
    return sess


def save_session(sid: str, sess: dict) -> None:
    """显式持久化 session。所有写操作后必须调用。"""
    sess["ts"] = time.time()
    _write_session(sid, sess)


def _write_session(sid: str, sess: dict) -> None:
    _SESSION_DIR.mkdir(parents=True, exist_ok=True)
    p = _session_path(sid)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sess, f, ensure_ascii=False)
    os.replace(tmp, p)  # 原子替换，Windows 上 os.replace 也是原子的
