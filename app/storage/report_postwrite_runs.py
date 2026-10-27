"""Atomic, immutable-input run files under DATA_DIR; no application decisions."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import uuid
from time import sleep
from app.core.config import DATA_DIR
from app.core.runtime_logging import runtime_log


_REPLACE_RETRY_DELAYS = (0.02, 0.05)
_RETRYABLE_WINDOWS_ERRORS = {5, 32, 33}


class RunStore:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else Path(DATA_DIR) / "report_postwrite_runs"

    def _path(self, run_id):
        if not re.fullmatch(r"[a-f0-9]{32}", str(run_id)):
            raise ValueError("无效的核验运行编号")
        return self.root / f"{run_id}.json"

    def create(self, inputs, state=None):
        run_id = uuid.uuid4().hex
        self.save(run_id, {"inputs": deepcopy(inputs), "state": deepcopy(state or {})}, create=True)
        return run_id

    def load(self, run_id):
        return json.loads(self._path(run_id).read_text(encoding="utf-8"))

    def save(self, run_id, value, *, create=False):
        target = self._path(run_id)
        self.root.mkdir(parents=True, exist_ok=True)
        if create and target.exists():
            raise ValueError("核验运行已存在")
        if not create:
            previous = self.load(run_id)
            if value.get("inputs") != previous.get("inputs"):
                raise ValueError("冻结输入不能修改")
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        attempt = 1
        stage = "写入临时检查点"
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            stage = "替换核验检查点"
            for attempt in range(1, len(_REPLACE_RETRY_DELAYS) + 2):
                try:
                    os.replace(temporary, target)
                except OSError as exc:
                    if (getattr(exc, "winerror", None) not in _RETRYABLE_WINDOWS_ERRORS
                            or attempt > len(_REPLACE_RETRY_DELAYS)):
                        raise
                    if attempt == 1:
                        runtime_log(
                            "核验进度", "warning", "核验检查点暂未能替换，正在有限重试；只重试本地保存，不重新调用模型",
                            run_id=run_id, stage=stage, attempt=attempt,
                            **{"Windows 错误码": exc.winerror},
                        )
                    sleep(_REPLACE_RETRY_DELAYS[attempt - 1])
                else:
                    if attempt > 1:
                        runtime_log("核验进度", "success", "核验检查点已在本地重试后保存",
                                    run_id=run_id, stage=stage, attempt=attempt)
                    break
        except OSError as exc:
            runtime_log(
                "核验进度", "error", "核验检查点未能保存，未覆盖已有检查点；请稍后补全或联系管理员检查文件访问状态",
                run_id=run_id, stage=stage, attempt=attempt, error_type=type(exc).__name__,
                **{"系统错误码": exc.errno, "Windows 错误码": getattr(exc, "winerror", None)},
            )
            raise
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                runtime_log(
                    "核验进度", "warning", "核验临时文件暂未能清理，请管理员检查文件访问状态；本次保存结果保持原样",
                    run_id=run_id, stage="清理核验临时文件", error_type=type(exc).__name__,
                    **{"系统错误码": exc.errno, "Windows 错误码": getattr(exc, "winerror", None)},
                )
