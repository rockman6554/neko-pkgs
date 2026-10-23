import importlib.util
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "import-next.py"
SPEC = importlib.util.spec_from_file_location("next_importer", SCRIPT)
importer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = importer
SPEC.loader.exec_module(importer)


class NextListTests(unittest.TestCase):
    def test_reads_simple_and_explicit_candidates(self):
        content = (
            "# pendientes\n"
            "https://github.com/owner/tool\n"
            "owner/source custom-name source cmake --ignore-license\n"
            "owner/bundled bundled bundle bundled\n"
        )
        with tempfile.TemporaryDirectory() as tmp_name:
            path = Path(tmp_name) / "next.list"
            path.write_text(content, encoding="utf-8")
            comments, candidates = importer.read_candidates(path)
        self.assertEqual(comments, ["# pendientes"])
        self.assertEqual(candidates[0].repo, "owner/tool")
        self.assertEqual(candidates[0].pkgname, "tool")
        self.assertEqual(candidates[0].requested_kind, "auto")
        self.assertFalse(candidates[0].ignore_license)
        self.assertEqual(candidates[1].pkgname, "custom-name")
        self.assertEqual(candidates[1].requested_kind, "source")
        self.assertEqual(candidates[1].requested_target, "cmake")
        self.assertTrue(candidates[1].ignore_license)
        self.assertEqual(candidates[2].requested_kind, "bundle")
        self.assertEqual(candidates[2].requested_target, "bundled")

    def test_reads_ignore_license_without_other_optional_fields(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            path = Path(tmp_name) / "next.list"
            path.write_text("owner/tool --ignore-license\n", encoding="utf-8")
            _, candidates = importer.read_candidates(path)
        self.assertEqual(candidates[0].pkgname, "tool")
        self.assertTrue(candidates[0].ignore_license)

    def test_reading_registry_preserves_its_contents(self):
        content = "owner/tool\nowner/app app bundle app --ignore-license\n"
        with tempfile.TemporaryDirectory() as tmp_name:
            path = Path(tmp_name) / "next.list"
            path.write_text(content, encoding="utf-8")
            importer.read_candidates(path)
            preserved = path.read_text(encoding="utf-8")
        self.assertEqual(preserved, content)

    def test_rejects_unknown_option(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            path = Path(tmp_name) / "next.list"
            path.write_text("owner/tool --ignore-all\n", encoding="utf-8")
            with self.assertRaises(importer.generator.GeneratorError):
                importer.read_candidates(path)

    def test_rejects_ignore_option_without_repository(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            path = Path(tmp_name) / "next.list"
            path.write_text("--ignore-license\n", encoding="utf-8")
            with self.assertRaises(importer.generator.GeneratorError):
                importer.read_candidates(path)

    def test_ignore_missing_github_license_uses_custom_marker(self):
        repo_data = {"license": {"spdx_id": "NOASSERTION"}}
        self.assertEqual(
            importer.github_license(repo_data, ignore_missing=True),
            "custom:unknown",
        )

    def test_missing_github_license_still_fails_by_default(self):
        repo_data = {"license": {"spdx_id": "NOASSERTION"}}
        with self.assertRaises(importer.generator.GeneratorError):
            importer.github_license(repo_data)

    def test_generates_version_independent_asset_pattern(self):
        pattern = importer.generated_asset_pattern(
            "tool-v1.2.3-linux-x86_64.tar.gz", "1.2.3"
        )
        self.assertRegex("tool-v9.8.7-linux-x86_64.tar.gz", pattern)
        self.assertNotRegex("tool-v9.8.7-linux-arm64.tar.gz", pattern)


class RegistryPruningTests(unittest.TestCase):
    @staticmethod
    def recipe() -> importer.generator.Recipe:
        return importer.generator.Recipe(
            "tool",
            "owner/tool",
            r"^tool-.*-linux-x86_64\.tar\.gz$",
            "binary",
            "tool",
            "-",
            "MIT",
            "Tool",
        )

    def prepare_registry(self, root: Path) -> Path:
        path = root / "tarballs.tsv"
        path.write_text(
            "# registry\n" + importer.recipe_to_tsv(self.recipe()) + "\n",
            encoding="utf-8",
        )
        return path

    def test_removes_generated_package_missing_from_next_list(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            registry = self.prepare_registry(root)
            package = root / "tool"
            package.mkdir()
            (package / "template").write_text(
                importer.generator.GENERATED_MARKER + "\n",
                encoding="utf-8",
            )

            retained, removed, failures = importer.prune_unregistered_recipes(
                registry, [self.recipe()], set(), root
            )

            self.assertEqual(retained, [])
            self.assertEqual(removed, ["tool"])
            self.assertEqual(failures, [])
            self.assertFalse(package.exists())
            self.assertEqual(registry.read_text(encoding="utf-8"), "# registry\n")

    def test_keeps_recipe_that_is_still_registered(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            registry = self.prepare_registry(root)
            package = root / "tool"
            package.mkdir()
            template = package / "template"
            template.write_text(
                importer.generator.GENERATED_MARKER + "\n",
                encoding="utf-8",
            )

            retained, removed, failures = importer.prune_unregistered_recipes(
                registry, [self.recipe()], {"owner/tool"}, root
            )

            self.assertEqual(retained, [self.recipe()])
            self.assertEqual(removed, [])
            self.assertEqual(failures, [])
            self.assertTrue(template.exists())
            self.assertIn("owner/tool", registry.read_text(encoding="utf-8"))

    def test_does_not_remove_manual_template(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            registry = self.prepare_registry(root)
            package = root / "tool"
            package.mkdir()
            template = package / "template"
            template.write_text("# maintained manually\n", encoding="utf-8")

            retained, removed, failures = importer.prune_unregistered_recipes(
                registry, [self.recipe()], set(), root
            )

            self.assertEqual(retained, [self.recipe()])
            self.assertEqual(removed, [])
            self.assertEqual(len(failures), 1)
            self.assertTrue(template.exists())
            self.assertIn("owner/tool", registry.read_text(encoding="utf-8"))

    def test_does_not_remove_package_with_additional_files(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            registry = self.prepare_registry(root)
            package = root / "tool"
            package.mkdir()
            template = package / "template"
            template.write_text(
                importer.generator.GENERATED_MARKER + "\n",
                encoding="utf-8",
            )
            (package / "patches").mkdir()

            retained, removed, failures = importer.prune_unregistered_recipes(
                registry, [self.recipe()], set(), root
            )

            self.assertEqual(retained, [self.recipe()])
            self.assertEqual(removed, [])
            self.assertEqual(len(failures), 1)
            self.assertTrue(template.exists())
            self.assertTrue((package / "patches").exists())
            self.assertIn("owner/tool", registry.read_text(encoding="utf-8"))


class ExecutableDiscoveryTests(unittest.TestCase):
    @staticmethod
    def elf_x86_64() -> bytes:
        header = bytearray(64)
        header[:4] = b"\x7fELF"
        header[4] = 2
        header[5] = 1
        header[18:20] = (62).to_bytes(2, "little")
        return bytes(header)

    def test_discovers_main_elf(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            archive_path = Path(tmp_name) / "tool-linux-x86_64.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                for name in ("tool-1.0/tool", "tool-1.0/lib/helper"):
                    payload = self.elf_x86_64()
                    info = tarfile.TarInfo(name)
                    info.mode = 0o755
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
            executable = importer.discover_executable(
                archive_path, archive_path.name, "", "tool"
            )
        self.assertEqual(executable.name, "tool")
        self.assertIsNone(executable.bundle_root)

    def test_detects_application_bundle(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            archive_path = Path(tmp_name) / "tool-linux-x86_64.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                payload = self.elf_x86_64()
                info = tarfile.TarInfo("tool.app/bin/tool")
                info.mode = 0o755
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            executable = importer.discover_executable(
                archive_path, archive_path.name, "tool", "tool"
            )
        self.assertEqual(executable.name, "tool")
        self.assertEqual(executable.bundle_root, "tool.app")


if __name__ == "__main__":
    unittest.main()
