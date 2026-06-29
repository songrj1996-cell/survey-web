#!/usr/bin/env python3
"""分层边界检查脚本。

规则：
  - app/routers/*  不得直接 import app.storage 或 app.integrations
  - app/core/*     不得 import app.services 或 app.routers
  - app/storage/*  不得 import app.services 或 app.routers
  - app/integrations/* 不得 import app.services 或 app.routers

Usage:
    python scripts/check_boundaries.py
"""
import ast
import pathlib
import sys

APP = pathlib.Path(__file__).parent.parent / "app"

RULES = [
    # (glob 模式, 禁止 import 的前缀列表, 规则描述)
    (
        "routers/*.py",
        ["app.storage", "app.integrations"],
        "router 不应直接依赖 storage / integrations",
    ),
    (
        "core/*.py",
        ["app.services", "app.routers"],
        "core 不应依赖 services / routers",
    ),
    (
        "storage/*.py",
        ["app.services", "app.routers"],
        "storage 不应依赖 services / routers",
    ),
    (
        "integrations/*.py",
        ["app.services", "app.routers"],
        "integrations 不应依赖 services / routers",
    ),
]


def collect_imports(src: str, path: pathlib.Path) -> list[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        print(f"FAIL: syntax error in {path}: {e}")
        sys.exit(1)
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                mods.append(alias.name)
    return mods


violations: list[str] = []
for glob_pat, forbidden, desc in RULES:
    for path in sorted(APP.glob(glob_pat)):
        src = path.read_text(encoding="utf-8")
        for mod in collect_imports(src, path):
            for prefix in forbidden:
                if mod == prefix or mod.startswith(prefix + "."):
                    rel = path.relative_to(APP.parent)
                    violations.append(f"  {rel}: imports '{mod}'  [{desc}]")

if violations:
    print("FAIL: boundary check failed:")
    for v in violations:
        print(v)
    sys.exit(1)
else:
    print("OK: boundary check passed, all layer dependencies are compliant.")
