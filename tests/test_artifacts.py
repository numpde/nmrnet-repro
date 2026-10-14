"""Offline acquisition boundaries, using small synthetic public artifacts."""

import hashlib
from contextlib import contextmanager
import io
import json
from pathlib import Path
import tempfile
import struct
import unittest
from unittest.mock import patch
import zipfile

from tools import artifacts


class ReleaseDeclarationTests(unittest.TestCase):
    """Unknown declaration authority must fail before artifact acquisition."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "release.json"
        self.document = {
            "record_id": "1", "doi": "10.5281/zenodo.1", "version_index": 0,
            "publication_date": "2026-01-18", "metadata_url": "https://zenodo.org/api/records/1",
            "files": [{"name": "fixture.zip", "size": 100, "md5": "0" * 32,
                       "url": "https://zenodo.org/api/records/1/files/fixture.zip/content"}],
        }

    def load(self):
        self.path.write_text(json.dumps(self.document))
        return artifacts.load_declaration(self.path)

    def test_valid_declaration_retains_every_field(self):
        self.assertEqual(self.load(), self.document)

    def test_unknown_root_field_rejects_declaration(self):
        self.document["checksum_override"] = "unreviewed"
        with self.assertRaisesRegex(ValueError, "Cannot load release: fields"):
            self.load()

    def test_unknown_archive_field_rejects_declaration(self):
        self.document["files"][-1]["url_override"] = "unreviewed"
        with self.assertRaisesRegex(ValueError, "Cannot load release: archive fields"):
            self.load()

    def test_missing_root_field_rejects_declaration(self):
        del self.document["metadata_url"]
        with self.assertRaisesRegex(ValueError, "Cannot load release: fields"):
            self.load()

    def test_missing_archive_field_rejects_declaration(self):
        del self.document["files"][0]["md5"]
        with self.assertRaisesRegex(ValueError, "Cannot load release: archive fields"):
            self.load()

    def test_non_object_declarations_and_non_list_files_reject_cleanly(self):
        for document in ([], {**self.document, "files": {}},
                         {**self.document, "files": [None]}):
            with self.subTest(document=document):
                self.document = document
                with self.assertRaisesRegex(ValueError, "Cannot load release:"):
                    self.load()


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def read(self, size=-1):
        if size < 0 or size > 1024 * 1024:
            raise AssertionError(f"Download must bound reads; requested {size} bytes")
        return super().read(size)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "fixture.zip"
        self.output = self.root / "prepared"
        self.selection = {"members": ["model/weights.pt"], "max_bytes": 100}
        self.make_zip([("model/weights.pt", b"checkpoint"), ("other", b"other")])

    def make_zip(self, members):
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, body in members:
                archive.writestr(name, body)
        self.body = self.archive.read_bytes()
        self.declaration = {
            "name": "fixture.zip", "size": len(self.body),
            "md5": hashlib.md5(self.body).hexdigest(),
            "url": "https://zenodo.org/records/18232165/files/fixture.zip?download=1",
        }

    def test_modified_and_truncated_archives_fail_before_output(self):
        for body in (self.body[:-1], bytes([self.body[0] ^ 1]) + self.body[1:]):
            with self.subTest(size=len(body)):
                self.archive.write_bytes(body)
                with self.assertRaises(ValueError):
                    artifacts.extract(self.archive, self.declaration, self.selection, self.output)
                self.assertFalse(self.output.exists())

    def test_verified_but_incomplete_zip_is_rejected(self):
        self.archive.write_bytes(self.body[:-22])
        self.declaration.update(size=self.archive.stat().st_size,
                                md5=hashlib.md5(self.archive.read_bytes()).hexdigest())
        with self.assertRaises((ValueError, zipfile.BadZipFile)):
            artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_required_member_does_not_publish(self):
        self.selection["members"].append("missing")
        with self.assertRaisesRegex(ValueError, "missing"):
            artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertFalse(self.output.exists())

    def test_unsafe_unselected_members_are_rejected(self):
        for name in ("../escape", "/absolute", "a/../escape", "a\\escape"):
            with self.subTest(name=name):
                self.make_zip([("model/weights.pt", b"checkpoint"), (name, b"bad")])
                with self.assertRaises(ValueError):
                    artifacts.extract(self.archive, self.declaration, self.selection, self.output)
                self.assertFalse(self.output.exists())
                self.assertFalse((self.root / "escape").exists())

    def test_symlink_member_rejected(self):
        member = zipfile.ZipInfo("model/weights.pt")
        member.create_system = 3
        member.external_attr = 0o120777 << 16
        self.make_zip([(member, b"/outside")])
        with self.assertRaises(ValueError):
            artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertFalse(self.output.exists())

    def test_directory_mode_without_directory_path_is_rejected(self):
        member = zipfile.ZipInfo("model/weights.pt")
        member.create_system = 3
        member.external_attr = 0o40755 << 16
        self.make_zip([(member, b"not a regular file")])
        with self.assertRaises(ValueError):
            artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertFalse(self.output.exists())

    def test_directory_entry_count_cannot_hide_actual_member_count(self):
        self.make_zip([("model/weights.pt", b"checkpoint"), ("other", b"other")])
        body = bytearray(self.body)
        end = body.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", body, end + 8, 1, 1)
        self.archive.write_bytes(body)
        self.declaration.update(size=len(body), md5=hashlib.md5(body).hexdigest())
        with patch.object(artifacts, "MAX_MEMBERS", 1):
            with self.assertRaises(ValueError):
                artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertFalse(self.output.exists())

    def add_zip64_directory(self, directory_size):
        end = self.body.rfind(b"PK\x05\x06")
        fields = struct.unpack_from("<4s4H2LH", self.body, end)
        record = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45,
                             0, 0, fields[4], fields[4], directory_size, fields[6])
        locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, end, 1)
        body = self.body[:end] + record + locator + self.body[end:]
        self.archive.write_bytes(body)
        self.declaration.update(size=len(body), md5=hashlib.md5(body).hexdigest())

    def test_zip64_directory_override_is_bounded_without_sentinels(self):
        self.add_zip64_directory(artifacts.MAX_DIRECTORY_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "directory exceeds"):
            artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertFalse(self.output.exists())

    def test_zip64_directory_without_sentinels_is_supported(self):
        end = self.body.rfind(b"PK\x05\x06")
        size = struct.unpack_from("<L", self.body, end + 12)[0]
        self.add_zip64_directory(size)
        artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertEqual((self.output / "model/weights.pt").read_bytes(), b"checkpoint")

    def test_replacing_archive_path_cannot_change_verified_extraction(self):
        replacement = self.root / "replacement.zip"
        with zipfile.ZipFile(replacement, "w") as archive:
            archive.writestr("model/weights.pt", b"tampered")
        original = artifacts.regular_file
        @contextmanager
        def replace_after_close(path, flags):
            with original(path, flags) as stream:
                yield stream
            if path == self.archive and replacement.exists():
                replacement.replace(path)
        with patch.object(artifacts, "regular_file", replace_after_close):
            artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertEqual((self.output / "model/weights.pt").read_bytes(), b"checkpoint")
        receipt = json.loads((self.output / "provenance.json").read_text())
        self.assertEqual(receipt["archive_sha256"], hashlib.sha256(self.body).hexdigest())

    def test_duplicate_member_rejected(self):
        self.make_zip([("model/weights.pt", b"a"), ("model/weights.pt", b"b")])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertFalse(self.output.exists())

    def test_output_size_limit_is_enforced(self):
        self.selection["max_bytes"] = 2
        with self.assertRaisesRegex(ValueError, "limit|bytes"):
            artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertFalse(self.output.exists())

    def test_unbounded_compression_methods_are_rejected_before_member_read(self):
        for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            with self.subTest(method=method):
                member = zipfile.ZipInfo("model/weights.pt")
                member.compress_type = method
                self.make_zip([(member, b"A" * (2 * artifacts.CHUNK_BYTES))])
                body = bytearray(self.body)
                directory = body.index(b"PK\x01\x02")
                # Claim only the first byte, including its matching CRC, so the
                # declared size cannot expose expansion hidden by ZipExtFile.
                crc = zipfile.crc32(b"A")
                struct.pack_into("<L", body, 14, crc)
                struct.pack_into("<L", body, 22, 1)
                struct.pack_into("<L", body, directory + 16, crc)
                struct.pack_into("<L", body, directory + 24, 1)
                self.archive.write_bytes(body)
                self.declaration.update(size=len(body), md5=hashlib.md5(body).hexdigest())
                self.selection["max_bytes"] = 1
                with patch.object(zipfile.ZipExtFile, "read", side_effect=AssertionError("member read started")):
                    with self.assertRaisesRegex(ValueError, "compression method"):
                        artifacts.extract(self.archive, self.declaration, self.selection, self.output)
                self.assertFalse(self.output.exists())

    def test_stored_and_deflated_members_are_supported(self):
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(method=method):
                member = zipfile.ZipInfo("model/weights.pt")
                member.compress_type = method
                self.make_zip([(member, b"checkpoint")])
                output = self.root / f"prepared-{method}"
                artifacts.extract(self.archive, self.declaration, self.selection, output)
                self.assertEqual((output / "model/weights.pt").read_bytes(), b"checkpoint")

    def test_extract_records_source_and_selected_sha256(self):
        artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertEqual((self.output / "model/weights.pt").read_bytes(), b"checkpoint")
        self.assertFalse((self.output / "other").exists())
        receipt = json.loads((self.output / "provenance.json").read_text())
        self.assertEqual(receipt["archive"], self.declaration)
        self.assertEqual(receipt["members"]["model/weights.pt"]["sha256"],
                         hashlib.sha256(b"checkpoint").hexdigest())
        producer_bytes = (Path(__file__).resolve().parents[1] / "tools/artifacts.py").read_bytes()
        self.assertEqual(receipt["producer_sources"], {"tools/artifacts.py": {
            "size": len(producer_bytes), "sha256": hashlib.sha256(producer_bytes).hexdigest()}})

    def test_existing_output_is_preserved(self):
        self.output.mkdir()
        sentinel = self.output / "keep"
        sentinel.write_bytes(b"installed")
        with self.assertRaises(FileExistsError):
            artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertEqual(sentinel.read_bytes(), b"installed")

    def test_interrupted_publication_has_no_complete_output(self):
        with patch.object(artifacts, "publish_directory", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertFalse(self.output.exists())

    def test_interrupted_extraction_does_not_publish_partial_members(self):
        original = zipfile.ZipExtFile.read
        def interrupted(source, size=-1):
            original(source, 1)
            raise OSError("extraction interrupted")
        with patch.object(zipfile.ZipExtFile, "read", interrupted):
            with self.assertRaises(OSError):
                artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertFalse(self.output.exists())

    def test_publication_refuses_even_an_empty_existing_directory(self):
        staged = self.root / "staged"
        staged.mkdir()
        (staged / "ready").write_bytes(b"verified")
        self.output.mkdir()
        with self.assertRaises(FileExistsError):
            artifacts.publish_directory(staged, self.output)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual((staged / "ready").read_bytes(), b"verified")

    def test_inspection_reports_names_and_sizes_after_verification(self):
        entries = artifacts.inspect_archive(self.archive, self.declaration)
        self.assertEqual({entry["name"]: entry["size"] for entry in entries},
                         {"model/weights.pt": 10, "other": 5})

    def test_resume_download_uses_matching_range_and_verifies(self):
        self.archive.unlink()
        partial = self.root / "fixture.zip.part"
        partial.write_bytes(self.body[:17])
        def open_response(request, **kwargs):
            self.assertEqual(request.get_header("Range"), "bytes=17-")
            return Response(self.body[17:], 206,
                            {"Content-Range": f"bytes 17-{len(self.body)-1}/{len(self.body)}"})
        artifacts.download(self.declaration, self.root, opener=open_response)
        self.assertEqual(self.archive.read_bytes(), self.body)
        self.assertFalse(partial.exists())

    def test_ignored_resume_range_preserves_partial(self):
        self.archive.unlink()
        partial = self.root / "fixture.zip.part"
        partial.write_bytes(self.body[:17])
        with self.assertRaisesRegex(ValueError, "range|Range"):
            artifacts.download(self.declaration, self.root,
                               opener=lambda *a, **kw: Response(self.body))
        self.assertEqual(partial.read_bytes(), self.body[:17])
        self.assertFalse(self.archive.exists())

    def test_wrong_resume_range_preserves_partial(self):
        self.archive.unlink()
        partial = self.root / "fixture.zip.part"
        partial.write_bytes(self.body[:17])
        for content_range in (f"bytes 0-{len(self.body)-1}/{len(self.body)}",
                              f"bytes 17-{len(self.body)-1}/{len(self.body)+1}"):
            with self.subTest(content_range=content_range):
                with self.assertRaisesRegex(ValueError, "range|Range"):
                    artifacts.download(self.declaration, self.root,
                        opener=lambda *a, **kw: Response(self.body[17:], 206,
                                                       {"Content-Range": content_range}))
                self.assertEqual(partial.read_bytes(), self.body[:17])
                self.assertFalse(self.archive.exists())

    def test_interrupted_download_preserves_resumable_partial(self):
        self.archive.unlink()
        class Interrupted(Response):
            def read(self, size=-1):
                if self.tell():
                    raise OSError("connection interrupted")
                return super().read(17)
        with self.assertRaises(OSError):
            artifacts.download(self.declaration, self.root,
                               opener=lambda *a, **kw: Interrupted(self.body))
        self.assertEqual((self.root / "fixture.zip.part").read_bytes(), self.body[:17])
        self.assertFalse(self.archive.exists())

    def test_download_rejects_wrong_checksum_and_excess_bytes(self):
        self.archive.unlink()
        for body in (b"X" * len(self.body), self.body + b"extra"):
            with self.subTest(size=len(body)):
                (self.root / "fixture.zip.part").unlink(missing_ok=True)
                with self.assertRaises(ValueError):
                    artifacts.download(self.declaration, self.root,
                                       opener=lambda *a, **kw: Response(body))
                self.assertFalse(self.archive.exists())
                self.assertLessEqual((self.root / "fixture.zip.part").stat().st_size,
                                     self.declaration["size"])

    def test_verified_download_is_reused_without_network(self):
        with patch.object(artifacts, "open_url", side_effect=AssertionError("network")):
            artifacts.download(self.declaration, self.root)
        self.assertEqual(self.archive.read_bytes(), self.body)

    def test_corrupt_existing_archive_refuses_network_and_replacement(self):
        self.archive.write_bytes(b"corrupted")
        with patch.object(artifacts, "open_url", side_effect=AssertionError("network")):
            with self.assertRaises(ValueError):
                artifacts.download(self.declaration, self.root)
        self.assertEqual(self.archive.read_bytes(), b"corrupted")

    def test_complete_partial_publishes_without_network(self):
        self.archive.unlink()
        (self.root / "fixture.zip.part").write_bytes(self.body)
        with patch.object(artifacts, "open_url", side_effect=AssertionError("network")):
            artifacts.download(self.declaration, self.root)
        self.assertEqual(self.archive.read_bytes(), self.body)
        self.assertFalse((self.root / "fixture.zip.part").exists())

    def test_partial_symlink_is_rejected_without_writing_target(self):
        self.archive.unlink()
        sentinel = self.root / "keep"
        sentinel.write_bytes(b"installed")
        (self.root / "fixture.zip.part").symlink_to(sentinel)
        with self.assertRaises((ValueError, OSError)):
            artifacts.download(self.declaration, self.root,
                               opener=lambda *a, **kw: Response(self.body))
        self.assertEqual(sentinel.read_bytes(), b"installed")


if __name__ == "__main__":
    unittest.main()
