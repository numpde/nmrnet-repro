"""Offline acquisition boundaries, using small synthetic public artifacts."""

import functools
import hashlib
import http.client
import io
import json
import os
import struct
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tools import artifacts


class ReleaseDeclarationTests(unittest.TestCase):
    """Unknown declaration authority must fail before artifact acquisition."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "release.json"
        self.document = {
            "record_id": "1",
            "doi": "10.5281/zenodo.1",
            "version_index": 0,
            "publication_date": "2026-01-18",
            "metadata_url": "https://zenodo.org/api/records/1",
            "files": [
                {
                    "name": "fixture.zip",
                    "size": 100,
                    "md5": "0" * 32,
                    "url": "https://zenodo.org/api/records/1/files/fixture.zip/content",
                }
            ],
        }

    def load(self):
        self.path.write_text(json.dumps(self.document))
        return artifacts.load_declaration(self.path)

    def test_valid_declaration_retains_every_field(self):
        self.assertEqual(self.document, self.load())

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
        for document in ([], {**self.document, "files": {}}, {**self.document, "files": [None]}):
            with self.subTest(document=document):
                self.document = document
                with self.assertRaisesRegex(ValueError, "Cannot load release:"):
                    self.load()


class Response(io.BytesIO):
    # silent-defaults: allow-contract -- This finite response declares ordinary HTTP 200 unless its caller supplies a different scenario status; proof=tests/test_artifacts.py::ArtifactTests.test_interrupted_download_preserves_resumable_partial
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        # silent-defaults: allow-contract -- Omitted fixture headers declare no fields; explicitly supplied fields remain the response's inputs; proof=tests/test_artifacts.py::ArtifactTests.test_interrupted_download_preserves_resumable_partial
        self.headers = headers or {}

    # silent-defaults: allow-contract -- The BytesIO-shaped signature uses -1 so omitted read size fails the bounded-read guard rather than consuming unbounded bytes; proof=tests/test_artifacts.py::ArtifactTests.test_download_rejects_wrong_checksum_and_excess_bytes
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
            "name": "fixture.zip",
            "size": len(self.body),
            "md5": hashlib.md5(self.body).hexdigest(),
            "url": "https://zenodo.org/records/18232165/files/fixture.zip?download=1",
        }

    def test_regular_file_preserves_binary_access_and_file_identity(self):
        path = self.root / "binary-file"
        original = b"\x00\xffbinary"
        replacement = b"\xfe\x00UPDATE"
        path.write_bytes(original)
        metadata = path.stat()
        identity = metadata.st_dev, metadata.st_ino
        with artifacts.regular_file(path, os.O_RDONLY) as stream:
            self.assertTrue(stream.readable())
            self.assertFalse(stream.writable())
            self.assertEqual(original, stream.read())
            with self.assertRaises(io.UnsupportedOperation):
                stream.write(replacement)
            metadata = os.fstat(stream.fileno())
            self.assertEqual(identity, (metadata.st_dev, metadata.st_ino))
        self.assertEqual(original, path.read_bytes())
        with artifacts.regular_file(path, os.O_RDWR) as stream:
            self.assertTrue(stream.readable())
            self.assertTrue(stream.writable())
            self.assertEqual(original, stream.read())
            stream.seek(0)
            self.assertEqual(len(replacement), stream.write(replacement))
            stream.flush()
            stream.seek(0)
            self.assertEqual(replacement, stream.read())
            metadata = os.fstat(stream.fileno())
            self.assertEqual(identity, (metadata.st_dev, metadata.st_ino))
        self.assertEqual(replacement, path.read_bytes())
        metadata = path.stat()
        self.assertEqual(identity, (metadata.st_dev, metadata.st_ino))

    def test_modified_and_truncated_archives_fail_before_output(self):
        for body in (self.body[:-1], bytes([self.body[0] ^ 1]) + self.body[1:]):
            with self.subTest(size=len(body)):
                self.archive.write_bytes(body)
                with self.assertRaises(ValueError):
                    artifacts.extract(self.archive, self.declaration, self.selection, self.output)
                self.assertFalse(self.output.exists())

    def test_verified_but_incomplete_zip_is_rejected(self):
        self.archive.write_bytes(self.body[:-22])
        self.declaration.update(size=self.archive.stat().st_size, md5=hashlib.md5(self.archive.read_bytes()).hexdigest())
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
        with patch.object(artifacts, "MAX_MEMBERS", 1), self.assertRaises(ValueError):
            artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertFalse(self.output.exists())

    def add_zip64_directory(self, directory_size):
        end = self.body.rfind(b"PK\x05\x06")
        fields = struct.unpack_from("<4s4H2LH", self.body, end)
        record = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, fields[4], fields[4], directory_size, fields[6])
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
        self.assertEqual(b"checkpoint", (self.output / "model/weights.pt").read_bytes())

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
        self.assertEqual(b"checkpoint", (self.output / "model/weights.pt").read_bytes())
        receipt = json.loads((self.output / "provenance.json").read_text())
        self.assertEqual(hashlib.sha256(self.body).hexdigest(), receipt["archive_sha256"])

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
                with (
                    patch.object(zipfile.ZipExtFile, "read", side_effect=AssertionError("member read started")),
                    self.assertRaisesRegex(ValueError, "compression method"),
                ):
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
                self.assertEqual(b"checkpoint", (output / "model/weights.pt").read_bytes())

    def test_extract_records_source_and_selected_sha256(self):
        artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertEqual(b"checkpoint", (self.output / "model/weights.pt").read_bytes())
        self.assertFalse((self.output / "other").exists())
        receipt = json.loads((self.output / "provenance.json").read_text())
        self.assertEqual(self.declaration, receipt["archive"])
        self.assertEqual(hashlib.sha256(b"checkpoint").hexdigest(), receipt["members"]["model/weights.pt"]["sha256"])
        producer_bytes = (Path(__file__).resolve().parents[1] / "tools/artifacts.py").read_bytes()
        self.assertEqual(
            {"tools/artifacts.py": {"size": len(producer_bytes), "sha256": hashlib.sha256(producer_bytes).hexdigest()}},
            receipt["producer_sources"],
        )

    def test_existing_output_is_preserved(self):
        self.output.mkdir()
        sentinel = self.output / "keep"
        sentinel.write_bytes(b"installed")
        with self.assertRaises(FileExistsError):
            artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertEqual(b"installed", sentinel.read_bytes())

    def test_interrupted_publication_has_no_complete_output(self):
        with patch.object(artifacts, "publish_directory", side_effect=OSError("interrupted")), self.assertRaises(OSError):
            artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertFalse(self.output.exists())

    def test_interrupted_extraction_does_not_publish_partial_members(self):
        original = zipfile.ZipExtFile.read

        def interrupted(source, _size):
            original(source, 1)
            raise OSError("extraction interrupted")

        with patch.object(zipfile.ZipExtFile, "read", interrupted), self.assertRaises(OSError):
            artifacts.extract(self.archive, self.declaration, self.selection, self.output)
        self.assertFalse(self.output.exists())

    def test_publication_refuses_even_an_empty_existing_directory(self):
        staged = self.root / "staged"
        staged.mkdir()
        (staged / "ready").write_bytes(b"verified")
        self.output.mkdir()
        with self.assertRaises(FileExistsError):
            artifacts.publish_directory(staged, self.output)
        self.assertEqual([], list(self.output.iterdir()))
        self.assertEqual(b"verified", (staged / "ready").read_bytes())

    def test_inspection_reports_names_and_sizes_after_verification(self):
        entries = artifacts.inspect_archive(self.archive, self.declaration)
        self.assertEqual({"model/weights.pt": 10, "other": 5}, {entry["name"]: entry["size"] for entry in entries})

    def test_http_content_coding_preserves_declared_archive_offsets(self):
        """Parse finite HTTP responses while download owns offsets and publication."""

        class WireSocket:
            def __init__(self, payload):
                self.payload = payload

            def makefile(self, mode):
                if mode != "rb":
                    raise AssertionError("HTTP response must open its finite wire bytes in binary mode")
                return io.BytesIO(self.payload)

        for coding, offset, corrupt in (
            (None, 0, False),
            ("identity", 0, False),
            (None, 17, False),
            ("identity", 17, False),
            ("gzip", 0, False),
            ("gzip", 17, False),
            ("identity", 17, True),
        ):
            with self.subTest(coding=coding, offset=offset, corrupt=corrupt):
                self.archive.unlink(missing_ok=True)
                partial = self.root / "fixture.zip.part"
                partial.write_bytes(self.body[:offset])
                before = (partial.stat().st_ino, partial.read_bytes())
                content = self.body[offset:]
                if corrupt:
                    content = bytes([content[0] ^ 1]) + content[1:]
                # silent-defaults: allow-contract -- The selected zero-offset wire case declares 200 and the resumed case declares 206; proof=tests/test_artifacts.py::ArtifactTests.test_http_content_coding_preserves_declared_archive_offsets
                status = 206 if offset else 200
                fields = [f"Content-Length: {len(content)}"]
                if offset:
                    fields.append(f"Content-Range: bytes {offset}-{len(self.body) - 1}/{len(self.body)}")
                if coding is not None:
                    fields.append(f"Content-Encoding: {coding}")
                # silent-defaults: allow-contract -- Each finite wire case pairs its selected 200 or 206 status with the matching literal reason phrase; proof=tests/test_artifacts.py::ArtifactTests.test_http_content_coding_preserves_declared_archive_offsets
                reason = "Partial Content" if offset else "OK"
                wire = (f"HTTP/1.1 {status} {reason}\r\n" + "\r\n".join(fields) + "\r\n\r\n").encode("ascii") + content
                response = http.client.HTTPResponse(WireSocket(wire))
                response.begin()
                self.assertEqual(status, response.status)
                self.assertEqual(coding, response.headers.get("Content-Encoding"))

                def open_response(selected_offset, selected_response, request, *, timeout):
                    self.assertEqual(self.declaration["url"], request.full_url)
                    self.assertEqual("identity", request.get_header("Accept-encoding"))
                    # silent-defaults: allow-contract -- The selected zero-offset case expects no Range header and the resumed case expects exactly bytes=17-; proof=tests/test_artifacts.py::ArtifactTests.test_http_content_coding_preserves_declared_archive_offsets
                    self.assertEqual("bytes=17-" if selected_offset else None, request.get_header("Range"))
                    self.assertEqual(60, timeout)
                    return selected_response

                with patch.object(
                    artifacts, "open_url", side_effect=functools.partial(open_response, offset, response)
                ) as opened:
                    if coding == "gzip":
                        with (
                            patch.object(response, "read", side_effect=AssertionError("encoded body must not be read")) as read,
                            self.assertRaisesRegex(ValueError, "encoded response cannot preserve archive offsets"),
                        ):
                            artifacts.download(self.declaration, self.root)
                        read.assert_not_called()
                        self.assertEqual(before, (partial.stat().st_ino, partial.read_bytes()))
                        self.assertFalse(self.archive.exists())
                    elif corrupt:
                        with self.assertRaisesRegex(ValueError, "published MD5"):
                            artifacts.download(self.declaration, self.root)
                        self.assertEqual(before[0], partial.stat().st_ino)
                        self.assertEqual(b"", partial.read_bytes())
                        self.assertFalse(self.archive.exists())
                    else:
                        artifacts.download(self.declaration, self.root)
                        self.assertEqual(self.body, self.archive.read_bytes())
                        self.assertEqual(before[0], self.archive.stat().st_ino)
                        self.assertFalse(partial.exists())
                    opened.assert_called_once()

    def test_resume_download_uses_matching_range_and_verifies(self):
        self.archive.unlink()
        partial = self.root / "fixture.zip.part"
        partial.write_bytes(self.body[:17])

        def open_response(request, **kwargs):
            self.assertEqual("bytes=17-", request.get_header("Range"))
            return Response(self.body[17:], 206, {"Content-Range": f"bytes 17-{len(self.body) - 1}/{len(self.body)}"})

        artifacts.download(self.declaration, self.root, opener=open_response)
        self.assertEqual(self.body, self.archive.read_bytes())
        self.assertFalse(partial.exists())

    def test_ignored_resume_range_preserves_partial(self):
        self.archive.unlink()
        partial = self.root / "fixture.zip.part"
        partial.write_bytes(self.body[:17])
        with self.assertRaisesRegex(ValueError, "range|Range"):
            artifacts.download(self.declaration, self.root, opener=lambda *a, **kw: Response(self.body))
        self.assertEqual(self.body[:17], partial.read_bytes())
        self.assertFalse(self.archive.exists())

    def test_wrong_resume_range_preserves_partial(self):
        self.archive.unlink()
        partial = self.root / "fixture.zip.part"
        partial.write_bytes(self.body[:17])
        for content_range in (
            f"bytes 0-{len(self.body) - 1}/{len(self.body)}",
            f"bytes 17-{len(self.body) - 1}/{len(self.body) + 1}",
        ):
            with self.subTest(content_range=content_range):
                with self.assertRaisesRegex(ValueError, "range|Range"):
                    # Retain this iteration's range until the opener consumes it.
                    artifacts.download(
                        self.declaration,
                        self.root,
                        opener=functools.partial(
                            lambda content_range, *a, **kw: Response(self.body[17:], 206, {"Content-Range": content_range}),
                            content_range,
                        ),
                    )
                self.assertEqual(self.body[:17], partial.read_bytes())
                self.assertFalse(self.archive.exists())

    def test_interrupted_download_preserves_resumable_partial(self):
        self.archive.unlink()

        class Interrupted(Response):
            def read(self, _size):
                if self.tell():
                    raise OSError("connection interrupted")
                return super().read(17)

        with self.assertRaises(OSError):
            artifacts.download(self.declaration, self.root, opener=lambda *a, **kw: Interrupted(self.body))
        self.assertEqual(self.body[:17], (self.root / "fixture.zip.part").read_bytes())
        self.assertFalse(self.archive.exists())

    def test_download_rejects_wrong_checksum_and_excess_bytes(self):
        self.archive.unlink()
        for body in (b"X" * len(self.body), self.body + b"extra"):
            with self.subTest(size=len(body)):
                (self.root / "fixture.zip.part").unlink(missing_ok=True)
                with self.assertRaises(ValueError):
                    # Construct the response on invocation with this selected body.
                    artifacts.download(
                        self.declaration, self.root, opener=functools.partial(lambda body, *a, **kw: Response(body), body)
                    )
                self.assertFalse(self.archive.exists())
                self.assertLessEqual((self.root / "fixture.zip.part").stat().st_size, self.declaration["size"])

    def test_verified_download_is_reused_without_network(self):
        with patch.object(artifacts, "open_url", side_effect=AssertionError("network")):
            artifacts.download(self.declaration, self.root)
        self.assertEqual(self.body, self.archive.read_bytes())

    def test_corrupt_existing_archive_refuses_network_and_replacement(self):
        self.archive.write_bytes(b"corrupted")
        with patch.object(artifacts, "open_url", side_effect=AssertionError("network")), self.assertRaises(ValueError):
            artifacts.download(self.declaration, self.root)
        self.assertEqual(b"corrupted", self.archive.read_bytes())

    def test_complete_partial_publishes_without_network(self):
        self.archive.unlink()
        (self.root / "fixture.zip.part").write_bytes(self.body)
        with patch.object(artifacts, "open_url", side_effect=AssertionError("network")):
            artifacts.download(self.declaration, self.root)
        self.assertEqual(self.body, self.archive.read_bytes())
        self.assertFalse((self.root / "fixture.zip.part").exists())

    def test_rejected_full_size_partial_resets_for_a_zero_offset_retry(self):
        self.archive.unlink()
        partial = self.root / "fixture.zip.part"
        corrupt = bytes([self.body[0] ^ 1]) + self.body[1:]
        partial.write_bytes(corrupt)
        with (
            patch.object(artifacts, "open_url", side_effect=AssertionError("a complete partial needs no request")),
            self.assertRaisesRegex(ValueError, "published MD5") as failure,
        ):
            artifacts.download(self.declaration, self.root)
        self.assertIn("retry will start at byte zero", " ".join(failure.exception.__notes__))
        self.assertEqual(b"", partial.read_bytes())
        self.assertFalse(self.archive.exists())

        def open_retry(request, *, timeout):
            self.assertIsNone(request.get_header("Range"))
            self.assertEqual(60, timeout)
            return Response(self.body)

        artifacts.download(self.declaration, self.root, opener=open_retry)
        self.assertEqual(self.body, self.archive.read_bytes())
        self.assertFalse(partial.exists())

    def test_partial_symlink_is_rejected_without_writing_target(self):
        self.archive.unlink()
        sentinel = self.root / "keep"
        sentinel.write_bytes(b"installed")
        (self.root / "fixture.zip.part").symlink_to(sentinel)
        with self.assertRaises((ValueError, OSError)):
            artifacts.download(self.declaration, self.root, opener=lambda *a, **kw: Response(self.body))
        self.assertEqual(b"installed", sentinel.read_bytes())


if __name__ == "__main__":
    unittest.main()
