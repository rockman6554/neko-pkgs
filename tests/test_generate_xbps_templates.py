import importlib.util
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate-xbps-templates.py"
SPEC = importlib.util.spec_from_file_location("generator", SCRIPT)
generator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)


class AssetFilterTests(unittest.TestCase):
    def test_accepts_linux_x86_64_tarball(self):
        self.assertTrue(
            generator.is_compatible_asset_name(
                "tool-v1.2.3-x86_64-unknown-linux-gnu.tar.gz"
            )
        )

    def test_rejects_wrong_platforms_and_auxiliary_files(self):
        rejected = (
            "tool-windows-x86_64.tar.gz",
            "tool-linux-aarch64.tar.gz",
            "tool-freebsd-amd64.tar.gz",
            "tool-linux-amd64.tar.gz.sha256",
            "tool-source-linux-x86_64.tar.gz",
        )
        for name in rejected:
            with self.subTest(name=name):
                self.assertFalse(generator.is_compatible_asset_name(name))

    def test_prefers_gnu_over_musl(self):
        recipe = generator.Recipe(
            "tool",
            "owner/tool",
            r"^tool-.*\.tar\.gz$",
            "binary",
            "tool",
            "-",
            "MIT",
            "Tool",
        )
        assets = [
            generator.ReleaseAsset(
                "tool-linux-x86_64-musl.tar.gz", "https://example/musl", 1
            ),
            generator.ReleaseAsset(
                "tool-linux-x86_64-gnu.tar.gz", "https://example/gnu", 1
            ),
        ]
        self.assertEqual(generator.select_asset(recipe, assets).url, "https://example/gnu")


class ArchiveInspectionTests(unittest.TestCase):
    @staticmethod
    def elf_x86_64() -> bytes:
        header = bytearray(64)
        header[:4] = b"\x7fELF"
        header[4] = 2
        header[5] = 1
        header[18:20] = (62).to_bytes(2, "little")
        return bytes(header)

    def make_tarball(self, path: Path, member_name: str, payload: bytes) -> None:
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo(member_name)
            info.mode = 0o755
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    def test_finds_and_validates_x86_64_elf(self):
        recipe = generator.Recipe(
            "tool", "owner/tool", ".*", "binary", "tool", "-", "MIT", "Tool"
        )
        with tempfile.TemporaryDirectory() as tmp_name:
            archive = Path(tmp_name) / "tool-linux-x86_64.tar.gz"
            self.make_tarball(archive, "tool-1.0/bin/tool", self.elf_x86_64())
            result = generator.inspect_archive(archive, archive.name, recipe)
        self.assertEqual(result.executable_path, "tool-1.0/bin/tool")
        self.assertEqual(result.target_arch, "x86_64")

    def test_rejects_path_traversal(self):
        recipe = generator.Recipe(
            "tool", "owner/tool", ".*", "binary", "tool", "-", "MIT", "Tool"
        )
        with tempfile.TemporaryDirectory() as tmp_name:
            archive = Path(tmp_name) / "tool-linux-x86_64.tar.gz"
            self.make_tarball(archive, "../tool", self.elf_x86_64())
            with self.assertRaises(generator.GeneratorError):
                generator.inspect_archive(archive, archive.name, recipe)

    def test_rejects_non_elf_main_file(self):
        recipe = generator.Recipe(
            "tool", "owner/tool", ".*", "binary", "tool", "-", "MIT", "Tool"
        )
        with tempfile.TemporaryDirectory() as tmp_name:
            archive = Path(tmp_name) / "tool-linux-x86_64.tar.gz"
            self.make_tarball(archive, "tool", b"#!/bin/sh\n")
            with self.assertRaises(generator.GeneratorError):
                generator.inspect_archive(archive, archive.name, recipe)

    def test_accepts_source_tarball_with_declared_build_style(self):
        recipe = generator.Recipe(
            "tool", "owner/tool", ".*", "source", "-", "cmake", "MIT", "Tool"
        )
        with tempfile.TemporaryDirectory() as tmp_name:
            archive = Path(tmp_name) / "tool-1.0.tar.gz"
            self.make_tarball(archive, "tool-1.0/CMakeLists.txt", b"project(tool)\n")
            result = generator.inspect_archive(archive, archive.name, recipe)
        self.assertIsNone(result.executable_path)
        self.assertEqual(result.source_root, "tool-1.0")
        self.assertEqual(result.target_arch, "*")

    def test_rejects_source_without_declared_build_structure(self):
        recipe = generator.Recipe(
            "tool", "owner/tool", ".*", "source", "-", "meson", "MIT", "Tool"
        )
        with tempfile.TemporaryDirectory() as tmp_name:
            archive = Path(tmp_name) / "tool-1.0.tar.gz"
            self.make_tarball(archive, "tool-1.0/CMakeLists.txt", b"project(tool)\n")
            with self.assertRaises(generator.GeneratorError):
                generator.inspect_archive(archive, archive.name, recipe)


class RenderingTests(unittest.TestCase):
    def test_template_contains_xbps_fields(self):
        recipe = generator.Recipe(
            "tool",
            "owner/tool",
            ".*",
            "binary",
            "tool",
            "-",
            "MIT",
            "A useful command line tool",
        )
        asset = generator.ReleaseAsset(
            "tool-linux-x86_64.tar.gz",
            "https://github.com/owner/tool/releases/download/v1.2.3/tool-1.2.3-linux-x86_64.tar.gz",
            10,
        )
        inspected = generator.InspectedArchive("tool-1.2.3/tool", None, "x86_64")
        rendered = generator.render_template(
            recipe,
            "1.2.3",
            "https://github.com/owner/tool",
            asset,
            "a" * 64,
            inspected,
        )
        self.assertIn("version=1.2.3", rendered)
        self.assertIn("v${version}", rendered)
        self.assertIn('vbin "tool-1.2.3/tool" "tool"', rendered)
        self.assertIn(generator.GENERATED_MARKER, rendered)

    def test_source_template_contains_build_metadata(self):
        recipe = generator.Recipe(
            "tool",
            "owner/tool",
            "TARBALL",
            "source",
            "-",
            "cmake",
            "MIT",
            "A source package",
            hostmakedepends="pkg-config cmake",
            makedepends="libfoo-devel",
            depends="libfoo",
            configure_args="-DBUILD_TESTING=OFF",
        )
        asset = generator.ReleaseAsset(
            "tool-1.2.3.tar.gz",
            "https://github.com/owner/tool/archive/refs/tags/v1.2.3.tar.gz",
            10,
        )
        inspected = generator.InspectedArchive(
            None, "LICENSE", "*", source_root="tool-1.2.3"
        )
        rendered = generator.render_template(
            recipe,
            "1.2.3",
            "https://github.com/owner/tool",
            asset,
            "b" * 64,
            inspected,
        )
        self.assertIn("build_style=cmake", rendered)
        self.assertIn('wrksrc="tool-${version}"', rendered)
        self.assertIn('hostmakedepends="pkg-config cmake"', rendered)
        self.assertIn('makedepends="libfoo-devel"', rendered)
        self.assertIn('configure_args="-DBUILD_TESTING=OFF"', rendered)
        self.assertIn('vlicense "LICENSE"', rendered)
        self.assertNotIn("do_install()", rendered)


if __name__ == "__main__":
    unittest.main()
