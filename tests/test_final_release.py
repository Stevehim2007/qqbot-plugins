"""Final seven-plugin release contract tests."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins"
ARTIFACTS = ROOT / "artifacts"
FINAL_PLUGINS = {
    "qqbot_core_reply",
    "qqbot_safe_tools",
    "qqbot_web_reader",
    "qqbot_image_search",
    "qqbot_food",
    "qqbot_stickers",
    "qqbot_memory_commands",
}
RETIRED = {"qqbot_enhancer", "qqbot_core_full"}
REQUIRED_FILES = {"VERSION", "README.md", "metadata.yaml", "_conf_schema.json", "main.py"}
PLUGIN_TEST_TIMEOUT = 240


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def flat_metadata(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw or raw[:1].isspace() or raw.lstrip().startswith("#"):
            continue
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


class PluginLayoutTests(unittest.TestCase):
    def test_exact_final_plugin_set_and_no_retired_directories(self):
        present = {path.name for path in PLUGIN_ROOT.iterdir() if path.is_dir() and path.name.startswith("qqbot_")}
        self.assertEqual(present, FINAL_PLUGINS)
        self.assertFalse((PLUGIN_ROOT & RETIRED) if isinstance(PLUGIN_ROOT, set) else False)
        for retired in RETIRED:
            self.assertFalse((PLUGIN_ROOT / retired).exists(), retired)

    def test_plugin_metadata_schema_and_version_shape(self):
        for plugin_id in sorted(FINAL_PLUGINS):
            with self.subTest(plugin_id=plugin_id):
                directory = PLUGIN_ROOT / plugin_id
                missing = sorted(name for name in REQUIRED_FILES if not (directory / name).is_file())
                self.assertEqual(missing, [])
                version = (directory / "VERSION").read_text(encoding="utf-8-sig").strip()
                metadata = flat_metadata((directory / "metadata.yaml").read_text(encoding="utf-8-sig"))
                self.assertEqual(metadata.get("name"), plugin_id)
                self.assertEqual(metadata.get("version"), version)
                self.assertTrue(metadata.get("astrbot_version"))
                schema = json.loads((directory / "_conf_schema.json").read_text(encoding="utf-8-sig"))
                self.assertIsInstance(schema, dict)
                self.assertTrue(schema)
                self.assertTrue(all(isinstance(key, str) and isinstance(value, dict) for key, value in schema.items()))
                self.assertTrue((directory / "tests").is_dir())

    def test_no_retired_plugin_imports_in_python_sources(self):
        for plugin_id in sorted(FINAL_PLUGINS):
            for path in (PLUGIN_ROOT / plugin_id).rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                with self.subTest(path=str(path.relative_to(ROOT))):
                    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
                    for node in ast.walk(tree):
                        names: list[str] = []
                        if isinstance(node, ast.Import):
                            names = [alias.name for alias in node.names]
                        elif isinstance(node, ast.ImportFrom) and node.module:
                            names = [node.module]
                        for name in names:
                            self.assertFalse(name == "qqbot_enhancer" or name.startswith("qqbot_enhancer."))
                            self.assertFalse(name == "qqbot_core_full" or name.startswith("qqbot_core_full."))

    def test_plugin_constructor_accepts_injected_config(self):
        for plugin_id in sorted(FINAL_PLUGINS):
            with self.subTest(plugin_id=plugin_id):
                source = (PLUGIN_ROOT / plugin_id / "main.py").read_text(encoding="utf-8-sig")
                tree = ast.parse(source)
                found = False
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and node.name.endswith("Plugin"):
                        for item in node.body:
                            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                                args = [arg.arg for arg in item.args.args]
                                found = "config" in args or "config" in [arg.arg for arg in item.args.kwonlyargs]
                self.assertTrue(found, f"{plugin_id} Plugin __init__ must accept config")


class ArchiveContractTests(unittest.TestCase):
    def _catalog(self, name: str) -> dict:
        path = ROOT / name
        self.assertTrue(path.is_file(), name)
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        self.assertEqual(payload.get("schema_version"), 1)
        self.assertEqual(payload.get("catalog_id"), "qqbot-plugins")
        return payload

    def test_catalog_manifest_and_artifacts_match(self):
        catalog = self._catalog("catalog.json")
        manifest = self._catalog("manifest.json")
        local = catalog["plugins"]
        remote = manifest["plugins"]
        self.assertEqual({item["id"] for item in local}, FINAL_PLUGINS)
        self.assertEqual([item["id"] for item in local], [item["id"] for item in remote])
        self.assertEqual(len(list(ARTIFACTS.glob("*.zip"))), 7)
        for local_item, remote_item in zip(local, remote):
            plugin_id = local_item["id"]
            version = local_item["version"]
            expected_name = f"{plugin_id}-{version}.zip"
            archive = ARTIFACTS / expected_name
            self.assertTrue(archive.is_file(), expected_name)
            self.assertEqual(local_item["download"], expected_name)
            self.assertEqual(local_item["root"], plugin_id)
            self.assertEqual(local_item["size"], archive.stat().st_size)
            self.assertEqual(local_item["sha256"], sha256(archive))
            self.assertEqual(remote_item["sha256"], local_item["sha256"])
            self.assertEqual(remote_item["size"], local_item["size"])
            self.assertTrue(remote_item["download"].startswith("https://github.com/Stevehim2007/qqbot-plugins/releases/download/"))
            self.assertEqual(local_item["dependencies"], remote_item["dependencies"])
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
                roots = {name.split("/", 1)[0] for name in names if name}
                self.assertEqual(roots, {plugin_id})
                self.assertIn(f"{plugin_id}/metadata.yaml", names)
                self.assertIn(f"{plugin_id}/_conf_schema.json", names)
                self.assertIn(f"{plugin_id}/main.py", names)
                self.assertIn(f"{plugin_id}/VERSION", names)
                self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))

    def test_no_retired_archives_or_catalog_entries(self):
        for path in ARTIFACTS.glob("*.zip"):
            self.assertFalse(path.name.startswith("qqbot_enhancer-"), path.name)
            self.assertFalse(path.name.startswith("qqbot_core_full-"), path.name)
        for name in ("catalog.json", "manifest.json"):
            payload = self._catalog(name)
            ids = {item["id"] for item in payload["plugins"]}
            self.assertFalse(ids & RETIRED)
            self.assertEqual(len(payload["plugins"]), 7)


class PluginUnitSuiteTests(unittest.TestCase):
    def test_each_plugin_unit_suite(self):
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        for plugin_id in sorted(FINAL_PLUGINS):
            with self.subTest(plugin_id=plugin_id):
                result = subprocess.run(
                    [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
                    cwd=str(PLUGIN_ROOT / plugin_id),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=PLUGIN_TEST_TIMEOUT,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    msg=f"{plugin_id} tests failed\nSTDOUT:\n{result.stdout[-4000:]}\nSTDERR:\n{result.stderr[-4000:]}",
                )


if __name__ == "__main__":
    unittest.main()
