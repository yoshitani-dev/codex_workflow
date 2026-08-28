"""Regression tests for deterministic release packaging."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

import package_release


class DeterministicPackageTests(unittest.TestCase):
    def test_text_line_endings_do_not_change_archive_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "codex_workflow"
            package.mkdir()
            text_file = package / "sample.md"
            binary_file = package / "sample.bin"
            binary_file.write_bytes(b"\x00\r\n\xff")

            text_file.write_bytes(b"first\r\nsecond\r\n")
            windows_archive = root / "windows.zip"
            package_release.build_zip(package, windows_archive)

            text_file.write_bytes(b"first\nsecond\n")
            unix_archive = root / "unix.zip"
            package_release.build_zip(package, unix_archive)

            self.assertEqual(
                hashlib.sha256(windows_archive.read_bytes()).digest(),
                hashlib.sha256(unix_archive.read_bytes()).digest(),
            )
            with zipfile.ZipFile(windows_archive) as archive:
                self.assertEqual(
                    archive.read("codex_workflow/sample.md"), b"first\nsecond\n"
                )
                self.assertEqual(
                    archive.read("codex_workflow/sample.bin"), b"\x00\r\n\xff"
                )


if __name__ == "__main__":
    unittest.main()
