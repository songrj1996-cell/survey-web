"""Atomic, immutable-input run files under DATA_DIR; no application decisions."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import uuid
from app.core.config import DATA_DIR


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
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
