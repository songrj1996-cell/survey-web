"""同问卷分析预设的 JSON 持久化。

本模块只负责文件读取、schema 校验与原子写入。文件缺失时返回内存中的
空文档，读取操作不会创建或修复运行时文件。
"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import json
import os
import tempfile
import threading
import time
from typing import Any

from app.core import config


ANALYSIS_PRESETS_SCHEMA_VERSION = 1
_ANALYSIS_PRESETS_LOCK = threading.RLock()


class AnalysisPresetStorageError(RuntimeError):
    """分析预设文件无法被安全读取或保存。"""


def empty_analysis_presets() -> dict[str, Any]:
    """返回不会与模块内部状态共享引用的空存储文档。"""
    return {
        "schema_version": ANALYSIS_PRESETS_SCHEMA_VERSION,
        "revision": 0,
        "presets": [],
    }


def _preset_path() -> str:
    return os.fspath(config.ANALYSIS_PRESETS_FILE)


def _validate_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalysisPresetStorageError("分析预设存储格式无效")

    schema_version = value.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != ANALYSIS_PRESETS_SCHEMA_VERSION
    ):
        raise AnalysisPresetStorageError("分析预设 schema 版本不受支持")

    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise AnalysisPresetStorageError("分析预设 revision 格式无效")

    presets = value.get("presets")
    if not isinstance(presets, list) or any(
        not isinstance(preset, dict) for preset in presets
    ):
        raise AnalysisPresetStorageError("分析预设条目格式无效")

    return {
        "schema_version": ANALYSIS_PRESETS_SCHEMA_VERSION,
        "revision": revision,
        "presets": deepcopy(presets),
    }


def _load_unlocked() -> dict[str, Any]:
    path = _preset_path()
    if not os.path.exists(path):
        return empty_analysis_presets()
    try:
        with open(path, "r", encoding="utf-8") as source:
            value = json.load(source)
    except json.JSONDecodeError as exc:
        raise AnalysisPresetStorageError(
            "分析预设文件已损坏，请先恢复或修复该文件"
        ) from exc
    except OSError as exc:
        raise AnalysisPresetStorageError("分析预设文件读取失败") from exc
    return _validate_document(value)


def load_analysis_presets() -> dict[str, Any]:
    """读取防御性副本；文件缺失时不创建文件。"""
    with _ANALYSIS_PRESETS_LOCK:
        return _load_unlocked()


def _atomic_write_json(value: dict[str, Any]) -> None:
    path = _preset_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{os.path.basename(path)}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(value, temp_file, ensure_ascii=False, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        for attempt in range(5):
            try:
                os.replace(temp_path, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))
        temp_path = ""
    except OSError as exc:
        raise AnalysisPresetStorageError("分析预设文件保存失败") from exc
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass


def mutate_analysis_presets(
    mutator: Callable[[list[dict[str, Any]]], None],
) -> dict[str, Any]:
    """在同一把锁中读取、修改并原子保存预设列表。

    ``mutator`` 只接收预设列表的防御性副本，不能直接改写 schema 或
    revision。若读取、校验或回调失败，不会覆盖现有文件。
    """
    if not callable(mutator):
        raise TypeError("mutator must be callable")

    with _ANALYSIS_PRESETS_LOCK:
        current = _load_unlocked()
        presets = deepcopy(current["presets"])
        mutator(presets)
        candidate = _validate_document({
            "schema_version": ANALYSIS_PRESETS_SCHEMA_VERSION,
            "revision": current["revision"] + 1,
            "presets": presets,
        })
        _atomic_write_json(candidate)
        return deepcopy(candidate)
