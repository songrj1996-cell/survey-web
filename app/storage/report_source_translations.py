"""Atomic, content-addressed report-source translation cache; no session writes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from app.core import config


def _path(fingerprint: str) -> Path:
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise ValueError("invalid translation fingerprint")
    return Path(config.REPORT_SOURCE_TRANSLATIONS_DIR) / (fingerprint + ".json")


def load_source_translation(fingerprint: str) -> str | None:
    path = _path(fingerprint)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        return None
    text = value.get("translation_zh")
    if value.get("fingerprint") != fingerprint or not isinstance(text, str):
        return None
    if value.get("translation_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest():
        return None
    return text


def save_source_translation(fingerprint: str, translation_zh: str) -> None:
    path = _path(fingerprint)
    if not isinstance(translation_zh, str):
        raise ValueError("translation must be text")
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"schema_version": 1, "fingerprint": fingerprint,
             "translation_zh": translation_zh,
             "translation_sha256": hashlib.sha256(translation_zh.encode("utf-8")).hexdigest()}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".translation-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
