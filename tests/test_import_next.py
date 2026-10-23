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
            "owner/source custom-name source cmake\n"
        )
        with tempfile.TemporaryDirectory() as tmp_name:
            path = Path(tmp_name) / "next.list"
            path.write_text(content, encoding="utf-8")
            comments, candidates = importer.read_candidates(path)
        self.assertEqual(comments, ["# pendientes"])
        self.assertEqual(candidates[0].repo, "owner/tool")
        self.assertEqual(candidates[0].pkgname, "tool")
        self.assertEqual(candidates[0].requested_kind, "auto")
        self.assertEqual(candidates[1].pkgname, "custom-name")
        self.assertEqual(candidates[1].requested_kind, "source")
        self.assertEqual(candidates[1].requested_target, "cmake")

    def test_generates_version_independent_asset_pattern(self):
        pattern = importer.generated_asset_pattern(
            "tool-v1.2.3-linux-x86_64.tar.gz", "1.2.3"
        )
        self.assertRegex("tool-v9.8.7-linux-x86_64.tar.gz", pattern)
        self.assertNotRegex("tool-v9.8.7-linux-arm64.tar.gz", pattern)


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
        self.assertEqual(executable, "tool")


if __name__ == "__main__":
    unittest.main()
