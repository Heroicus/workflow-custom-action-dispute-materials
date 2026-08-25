#!/usr/bin/env python3
"""Deterministic release tests for organize-dispute-materials."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TEMPLATE = ROOT / "assets" / "reference-template.docx"


def run_script(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one package script with the package root as the working directory."""

    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class SkillContractTests(unittest.TestCase):
    """Verify production routing and field contracts are present and coherent."""

    def setUp(self) -> None:
        self.skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.runtime = (ROOT / "references" / "feishu-runtime-contract.md").read_text(encoding="utf-8")
        self.prompt = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")

    def test_frontmatter_and_required_sections(self) -> None:
        for marker in ("name:", "description:", "version:", "tier:", "category:", "dependencies:", "author:", "license:"):
            self.assertIn(marker, self.skill.split("---", 2)[1])
        for heading in ("# Name", "# Description", "## Features", "## Usage", "## Examples"):
            self.assertIn(heading, self.skill)
        self.assertRegex(self.skill, r"version:\s*3\.3\.3")

    def test_current_base_fields_only(self) -> None:
        for field in ("案件编号", "上传材料", "上传人", "AI分析结果", "状态", "执行日志/失败原因", "完成时间", "报告版本"):
            self.assertIn(field, self.skill)
            self.assertIn(field, self.runtime)
        self.assertIn("状态=分析中", self.skill)
        self.assertIn("状态=分析中", self.runtime)
        for obsolete in ("案件文档", "AI处理状态", "待法务审核"):
            self.assertNotIn(obsolete, self.skill)
            self.assertNotIn(obsolete, self.runtime)
            self.assertNotIn(obsolete, self.prompt)

    def test_exact_record_prompt_fails_fast_without_connector(self) -> None:
        self.assertIn("RUNTIME_INPUT_JSON", self.prompt)
        self.assertIn("table_id + record_id", self.prompt)
        self.assertIn("BASE_CONNECTOR_UNAVAILABLE", self.prompt)
        self.assertIn("禁止用 bash 搜索", self.prompt)
        self.assertNotIn("仅处理消息中的唯一案件编号", self.prompt)
        self.assertNotIn("在生产 Base 中精确匹配且只命中一条记录", self.prompt)
        self.assertIn("BASE_CONNECTOR_UNAVAILABLE", self.runtime)
        self.assertRegex(self.runtime, r"\| 定位键 \|[^\n]*record_id")
        self.assertNotRegex(self.runtime, r"\| 定位键 \|\s*案件编号\s*\|")

        for marker in (
            "permission_member_add",
            "permission_member_read",
            "full_access",
            '"member_type": "openid"',
            "member-type openid",
            "perm full_access",
            "上传人",
            "open_id",
        ):
            self.assertIn(marker, self.skill + self.runtime)
        self.assertIn("不得把上面的 CLI 文本当成已经执行", self.skill)
        self.assertIn("权限确认后", self.skill)

    def test_scope_and_output_boundaries(self) -> None:
        for marker in ("历史会话", "旧报告", "材料外", "原生飞书云文档", "同一 `record_id` 记录"):
            self.assertIn(marker, self.skill)
        self.assertNotIn("aixuexi.feishu.cn", self.skill + self.runtime + self.prompt)
        self.assertIn("不得把本地 Word 文件上传或导入成最终报告", self.runtime)
        self.assertIn("不得在 Skill 中写死某个租户域名", self.runtime)

    def test_reference_contracts_have_no_legacy_names(self) -> None:
        shipping_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in ROOT.rglob("*")
            if path.is_file() and path.name != "test_skill.py" and path.suffix.lower() in {".md", ".yaml", ".yml", ".json"}
        )
        for obsolete in ("案件文档", "AI处理状态", "待法务审核", "aixuexi.feishu.cn"):
            self.assertNotIn(obsolete, shipping_text)

    def test_release_tree_has_no_ide_state(self) -> None:
        forbidden = []
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT)
            if any(part == ".idea" for part in relative.parts):
                forbidden.append(relative.as_posix())
        self.assertEqual([], forbidden)
        self.assertTrue((ROOT / "README.md").is_file())


class ScriptAcceptanceTests(unittest.TestCase):
    """Exercise the validators against good and adversarial local fixtures."""

    def test_template_validator_detects_header_tampering(self) -> None:
        good = run_script("validate_template.py", str(TEMPLATE))
        self.assertEqual(0, good.returncode, good.stderr)
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "tampered.docx"
            with zipfile.ZipFile(TEMPLATE, "r") as source, zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as target:
                for info in source.infolist():
                    payload = source.read(info.filename)
                    if info.filename == "word/document.xml":
                        root = ET.fromstring(payload)
                        table = root.find(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tbl")
                        first_text = table.find(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
                        first_text.text = "已篡改"
                        payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                    target.writestr(info, payload)
            result = run_script("validate_template.py", str(bad))
            self.assertNotEqual(0, result.returncode)
            self.assertIn("first row mismatch", result.stderr)

    def test_inventory_skips_symbolic_links_and_absolute_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "case"
            root.mkdir()
            (root / "material.txt").write_text("current material", encoding="utf-8")
            outside = Path(directory) / "outside-target.txt"
            outside.write_text("outside", encoding="utf-8")
            (root / "outside-link.txt").symlink_to(outside)
            output = Path(directory) / "inventory.json"
            result = run_script(
                "inventory_attachments.py",
                "--input",
                str(root),
                "--output",
                str(output),
                "--case-id",
                "TEST-CASE",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("TEST-CASE", payload["case_id"])
            self.assertNotIn("input_root", payload)
            self.assertEqual(["material.txt"], [item["path"] for item in payload["files"]])
            self.assertEqual("symbolic_link", payload["skipped"][0]["reason"])

    def test_delivery_validator_requires_explicit_host_and_exact_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "url.txt"
            path.write_text("https://tenant.feishu.cn/docx/Abc_123", encoding="utf-8")
            good = run_script("validate_delivery.py", str(path), "--allowed-host", "tenant.feishu.cn")
            self.assertEqual(0, good.returncode, good.stderr)
            path.write_text("https://evil.example/docx/Abc_123", encoding="utf-8")
            bad_host = run_script("validate_delivery.py", str(path), "--allowed-host", "tenant.feishu.cn")
            self.assertNotEqual(0, bad_host.returncode)
            path.write_text("https://tenant.feishu.cn/docx/Abc_123?share=1", encoding="utf-8")
            bad_query = run_script("validate_delivery.py", str(path), "--allowed-host", "tenant.feishu.cn")
            self.assertNotEqual(0, bad_query.returncode)

    def test_packager_creates_clean_root_level_zip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "skill.zip"
            result = run_script("package_skill.py", "--source", str(ROOT), "--output", str(output), "--json")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(output.is_file())
            with zipfile.ZipFile(output, "r") as archive:
                names = archive.namelist()
            self.assertIn("SKILL.md", names)
            self.assertIn("agents/openai.yaml", names)
            self.assertFalse(any(name.startswith("organize-dispute-materials/") for name in names))
            self.assertFalse(any(any(part.startswith(".") for part in Path(name).parts) for name in names))
            self.assertFalse(any(".idea" in name for name in names))


if __name__ == "__main__":
    unittest.main(verbosity=2)
