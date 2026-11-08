#!/usr/bin/env python3
"""Build and verify the seven final qqbot-plugins release archives.

The repository is intentionally source-only.  This script creates one exact-root
ZIP per plugin, then regenerates the local catalog and the GitHub release
manifest from the archives actually written in this run.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins"
ARTIFACT_ROOT = ROOT / "artifacts"
FINAL_PLUGINS = (
    "qqbot_core_reply",
    "qqbot_safe_tools",
    "qqbot_web_reader",
    "qqbot_image_search",
    "qqbot_food",
    "qqbot_stickers",
    "qqbot_memory_commands",
)
PROVIDES: dict[str, list[str]] = {
    "qqbot_core_reply": ["reply"],
    "qqbot_safe_tools": ["tools", "tools.confirmation", "tools.audit"],
    "qqbot_web_reader": ["web.read", "web.read.next_page"],
    "qqbot_image_search": ["image.search", "image.search.next_page", "image.collect_plan"],
    "qqbot_food": ["food.query", "food.locations", "food.tags", "food.draft", "food.submit", "food.status"],
    "qqbot_stickers": ["sticker.collect", "sticker.categories", "sticker.send", "sticker.index"],
    "qqbot_memory_commands": ["memory.read", "memory.write", "memory.clear", "session", "persona"],
}
# Final release dependency direction:
#   safe_tools is the ABI/auth base and must not hard-depend on business providers;
#   business providers consume the base; core consumes safe_tools + memory.
REQUIRES: dict[str, list[dict[str, Any]]] = {
    "qqbot_core_reply": [
        {"plugin_id": "qqbot_safe_tools", "version_range": ">=3.0.0,<4", "capability": "tools", "abi_version": 1, "optional": False},
        {"plugin_id": "qqbot_memory_commands", "version_range": ">=4.0.0,<5", "capability": "memory.read", "abi_version": 1, "optional": False},
    ],
    "qqbot_safe_tools": [
        {"plugin_id": "qqbot_web_reader", "version_range": ">=2.0.0,<3", "capability": "web.read", "abi_version": 1, "optional": True},
        {"plugin_id": "qqbot_image_search", "version_range": ">=2.0.0,<3", "capability": "image.search", "abi_version": 1, "optional": True},
        {"plugin_id": "qqbot_food", "version_range": ">=3.0.0,<4", "capability": "food.query", "abi_version": 1, "optional": True},
        {"plugin_id": "qqbot_stickers", "version_range": ">=3.0.0,<4", "capability": "sticker.send", "abi_version": 1, "optional": True},
    ],
    "qqbot_web_reader": [
        {"plugin_id": "qqbot_safe_tools", "version_range": ">=3.0.0,<4", "capability": "tools", "abi_version": 1, "optional": False},
    ],
    "qqbot_image_search": [
        {"plugin_id": "qqbot_safe_tools", "version_range": ">=3.0.0,<4", "capability": "tools", "abi_version": 1, "optional": False},
        {"plugin_id": "qqbot_stickers", "version_range": ">=3.0.0,<4", "capability": "sticker.collect", "abi_version": 1, "optional": True},
    ],
    "qqbot_food": [
        {"plugin_id": "qqbot_safe_tools", "version_range": ">=3.0.0,<4", "capability": "tools", "abi_version": 1, "optional": False},
    ],
    "qqbot_stickers": [
        {"plugin_id": "qqbot_safe_tools", "version_range": ">=3.0.0,<4", "capability": "tools", "abi_version": 1, "optional": False},
    ],
    "qqbot_memory_commands": [
        {"plugin_id": "qqbot_safe_tools", "version_range": ">=3.0.0,<4", "capability": "tools", "abi_version": 1, "optional": False},
    ],
}
CONFLICTS = ["qqbot_enhancer", "qqbot_core_full"]
CONFIG_SCHEMA_VERSION = 1
DATA_SCHEMA_VERSION = 1
GROUP_REGISTRY_SCHEMA_VERSION = 1
DISPLAY_NAMES = {
    "qqbot_core_reply": "QQ 核心回复",
    "qqbot_safe_tools": "Safe Tools 协议",
    "qqbot_web_reader": "网页读取",
    "qqbot_image_search": "图片搜索",
    "qqbot_food": "Food 查询与提交",
    "qqbot_stickers": "表情收藏与发送",
    "qqbot_memory_commands": "记忆管理",
}
REQUIRED_FILES = {
    "VERSION",
    "README.md",
    "metadata.yaml",
    "_conf_schema.json",
    "main.py",
}
EXCLUDED_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
    ".DS_Store",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".tmp", ".swp"}
FORBIDDEN_IMPORT_RE = re.compile(
    r"(?m)^\s*(?:from|import)\s+(?:qqbot_enhancer|qqbot_core_full)(?:\s|\.|$)"
)
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+!()-]{0,79}$")
PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class BuildError(RuntimeError):
    pass


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise BuildError(f"无法读取 {path.relative_to(ROOT)}: {exc}") from exc


def _parse_metadata_simple(text: str) -> dict[str, str]:
    """Parse the flat top-level scalar keys used by metadata.yaml.

    The plugin metadata is deliberately flat; this avoids a third-party YAML
    dependency in the release build and fails closed on non-scalar top level.
    """
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if raw[:1].isspace() or line.startswith("- "):
            continue
        if ":" not in line:
            raise BuildError("metadata.yaml 顶层包含非 key: value 行")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


def _iter_plugin_files(plugin_dir: Path):
    for path in sorted(plugin_dir.rglob("*")):
        rel = path.relative_to(plugin_dir)
        if any(part in EXCLUDED_NAMES for part in rel.parts):
            continue
        if path.is_symlink():
            raise BuildError(f"{plugin_dir.name} 包含符号链接: {rel}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise BuildError(f"{plugin_dir.name} 包含特殊文件: {rel}")
        if path.suffix in EXCLUDED_SUFFIXES or path.name.endswith("~"):
            continue
        yield path, rel


def validate_plugin(plugin_id: str) -> dict[str, Any]:
    plugin_dir = PLUGIN_ROOT / plugin_id
    if not plugin_dir.is_dir() or plugin_dir.is_symlink():
        raise BuildError(f"缺少插件目录: {plugin_id}")
    missing = sorted(name for name in REQUIRED_FILES if not (plugin_dir / name).is_file())
    if missing:
        raise BuildError(f"{plugin_id} 缺少文件: {', '.join(missing)}")
    version = _read_text(plugin_dir / "VERSION").strip()
    if not VERSION_RE.fullmatch(version):
        raise BuildError(f"{plugin_id} VERSION 非法: {version!r}")
    metadata = _parse_metadata_simple(_read_text(plugin_dir / "metadata.yaml"))
    if metadata.get("name") != plugin_id:
        raise BuildError(f"{plugin_id} metadata.name 必须等于目录 ID")
    if metadata.get("version") != version:
        raise BuildError(f"{plugin_id} metadata.version 与 VERSION 不一致")
    if not metadata.get("astrbot_version"):
        raise BuildError(f"{plugin_id} metadata 缺少 astrbot_version")
    try:
        schema = json.loads(_read_text(plugin_dir / "_conf_schema.json"))
    except ValueError as exc:
        raise BuildError(f"{plugin_id} _conf_schema.json 不是合法 JSON") from exc
    if not isinstance(schema, dict) or not schema:
        raise BuildError(f"{plugin_id} _conf_schema.json 必须是非空 JSON object")
    for key, item in schema.items():
        if not isinstance(key, str) or not isinstance(item, dict):
            raise BuildError(f"{plugin_id} schema 字段 {key!r} 格式错误")

    tools: list[dict[str, Any]] = []
    tools_path = plugin_dir / "tools.json"
    if tools_path.is_file():
        try:
            tools_doc = json.loads(_read_text(tools_path))
        except ValueError as exc:
            raise BuildError(f"{plugin_id} tools.json 不是合法 JSON") from exc
        if (
            not isinstance(tools_doc, dict)
            or tools_doc.get("schema_version") != 1
            or tools_doc.get("plugin_id") != plugin_id
            or not isinstance(tools_doc.get("tools"), list)
        ):
            raise BuildError(f"{plugin_id} tools.json 结构无效")
        tools = [dict(item) for item in tools_doc["tools"] if isinstance(item, dict)]
        if len(tools) != len(tools_doc["tools"]):
            raise BuildError(f"{plugin_id} tools.json 包含非对象工具")
        for item in tools:
            if not item.get("name") or not item.get("method"):
                raise BuildError(f"{plugin_id} tools.json 工具缺少 name/method")

    py_files = []
    for path, rel in _iter_plugin_files(plugin_dir):
        if path.suffix == ".py":
            text = _read_text(path)
            if FORBIDDEN_IMPORT_RE.search(text):
                raise BuildError(f"{plugin_id}/{rel} 导入了退役插件")
            py_files.append(str(rel))
    if not py_files:
        raise BuildError(f"{plugin_id} 没有 Python 源码")
    tests_dir = plugin_dir / "tests"
    if not tests_dir.is_dir() or not any(tests_dir.glob("test_*.py")):
        raise BuildError(f"{plugin_id} 缺少 tests/test_*.py")
    return {
        "id": plugin_id,
        "name": DISPLAY_NAMES[plugin_id],
        "version": version,
        "description": metadata.get("desc", ""),
        "author": metadata.get("author", ""),
        "astrbot": metadata.get("astrbot_version", ""),
        # v1 compatibility field retained while clients migrate to requires.
        "dependencies": [
            item["plugin_id"] for item in REQUIRES[plugin_id] if not item.get("optional")
        ],
        "provides": [
            {"capability": capability, "abi_version": 1}
            for capability in PROVIDES[plugin_id]
        ],
        "requires": [dict(item) for item in REQUIRES[plugin_id]],
        "conflicts": list(CONFLICTS),
        "config_schema_version": CONFIG_SCHEMA_VERSION,
        "data_schema_version": DATA_SCHEMA_VERSION,
        "recommended_groups": ["QQ Bot"],
        **({"tools": tools} if tools else {}),
    }


def _zip_plugin(plugin_id: str, version: str) -> tuple[Path, str, int]:
    plugin_dir = PLUGIN_ROOT / plugin_id
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    final_path = ARTIFACT_ROOT / f"{plugin_id}-{version}.zip"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{plugin_id}-", suffix=".zip", dir=ARTIFACT_ROOT)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.writestr(f"{plugin_id}/", b"")
            for path, rel in _iter_plugin_files(plugin_dir):
                zf.write(path, f"{plugin_id}/{rel.as_posix()}")
        with zipfile.ZipFile(tmp_path) as zf:
            names = zf.namelist()
            roots = {name.split("/", 1)[0] for name in names if name}
            if roots != {plugin_id}:
                raise BuildError(f"{plugin_id} ZIP 顶层目录必须且只能是 {plugin_id}")
            if len(names) != len(set(names)):
                raise BuildError(f"{plugin_id} ZIP 存在重复条目")
            for name in names:
                parts = name.rstrip("/").split("/")
                if not name or name.startswith("/") or "\\" in name or any(p in {"", ".", ".."} for p in parts):
                    raise BuildError(f"{plugin_id} ZIP 包含不安全路径: {name!r}")
        os.replace(tmp_path, final_path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    digest = hashlib.sha256(final_path.read_bytes()).hexdigest()
    return final_path, digest, final_path.stat().st_size


def _catalog_entries(tag: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    local: list[dict[str, Any]] = []
    remote: list[dict[str, Any]] = []
    for plugin_id in FINAL_PLUGINS:
        meta = validate_plugin(plugin_id)
        archive, digest, size = _zip_plugin(plugin_id, meta["version"])
        base = {
            **meta,
            "sha256": digest,
            "size": size,
            "root": plugin_id,
        }
        local.append({**base, "download": archive.name})
        remote.append(
            {
                **base,
                "download": (
                    "https://github.com/Stevehim2007/qqbot-plugins/releases/download/"
                    f"{tag}/{archive.name}"
                ),
            }
        )
    return local, remote


def _group_param_registry() -> dict[str, Any]:
    """Build the frozen group-overridable parameter registry from ParamSpec."""
    module_path = PLUGIN_ROOT / "qqbot_safe_tools" / "capability_policy.py"
    spec = importlib.util.spec_from_file_location("_qqbot_capability_policy_for_build", module_path)
    if spec is None or spec.loader is None:
        raise BuildError("无法加载 capability_policy 参数规范")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    plugins: dict[str, dict[str, Any]] = {}
    for plugin_id in FINAL_PLUGINS:
        params: dict[str, Any] = {}
        for name, param in module.PLUGIN_PARAM_SPECS.get(plugin_id, {}).items():
            params[name] = {
                "kind": param.kind,
                "group_overridable": bool(param.group_overridable),
                "sensitive": bool(param.sensitive),
                "minimum": param.minimum,
                "maximum": param.maximum,
                "max_chars": param.max_chars,
                "enum": list(param.enum),
            }
        plugins[plugin_id] = params
    canonical = json.dumps(plugins, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": GROUP_REGISTRY_SCHEMA_VERSION,
        "registry_revision": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "plugins": plugins,
    }


def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def build(tag: str, *, clean: bool = True) -> dict[str, Any]:
    if not re.fullmatch(r"v[0-9A-Za-z][0-9A-Za-z._+-]{0,79}", tag):
        raise BuildError("release tag 非法")
    present = sorted(path.name for path in PLUGIN_ROOT.iterdir() if path.is_dir() and path.name.startswith("qqbot_"))
    if set(present) != set(FINAL_PLUGINS):
        extra = sorted(set(present) - set(FINAL_PLUGINS))
        missing = sorted(set(FINAL_PLUGINS) - set(present))
        raise BuildError(f"插件目录集合不正确；extra={extra}, missing={missing}")
    if clean and ARTIFACT_ROOT.exists():
        for old in ARTIFACT_ROOT.iterdir():
            if old.is_file() and old.suffix == ".zip":
                old.unlink()
    local, remote = _catalog_entries(tag)
    registry = _group_param_registry()
    _write_json(ROOT / "group_param_registry.json", registry)
    registry_ref = {
        "file": "group_param_registry.json",
        "registry_revision": registry["registry_revision"],
    }
    catalog = {
        "schema_version": 2,
        "catalog_id": "qqbot-plugins",
        "plugins": local,
        "group_param_registry": registry_ref,
    }
    manifest = {
        "schema_version": 2,
        "catalog_id": "qqbot-plugins",
        "plugins": remote,
        "group_param_registry": registry_ref,
    }
    _write_json(ROOT / "catalog.json", catalog)
    _write_json(ROOT / "manifest.json", manifest)
    return {
        "tag": tag,
        "plugins": local,
        "catalog": str(ROOT / "catalog.json"),
        "manifest": str(ROOT / "manifest.json"),
        "group_param_registry": str(ROOT / "group_param_registry.json"),
        "registry_revision": registry["registry_revision"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="GitHub release tag, e.g. v4.3.0-beta.4")
    parser.add_argument("--no-clean", action="store_true", help="keep old ZIPs in artifacts/")
    args = parser.parse_args(argv)
    try:
        result = build(args.tag, clean=not args.no_clean)
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    print(f"built {len(result['plugins'])} plugins for {result['tag']}")
    for item in result["plugins"]:
        print(f"{item['id']} {item['version']} {item['sha256']} {item['size']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
