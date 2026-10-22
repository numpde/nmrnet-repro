"""Malformed and excessive archives fail before any molecular parser receives bytes."""

import errno
import io
import stat
import struct
import unittest
import warnings
from unittest.mock import patch
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from nmrnet_inference.predict import MAX_INPUT_BYTES
from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.upload_archive import inspect_upload, select_member


def archive(entries):
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with ZipFile(stream, "w", compression=ZIP_STORED) as writer:
            for name, source in entries:
                writer.writestr(name, source)
    return stream.getvalue()


class UploadArchiveRefusalTests(unittest.TestCase):
    def test_missing_or_duplicate_member_never_selects_a_different_file(self):
        for entries, name in (((("a", b"x"), ("b", b"y")), "missing"), ((("a", b"x"), ("a", b"y")), "a")):
            with self.subTest(entries=entries), self.assertRaisesRegex(InputRejected, "missing|repeated"):
                select_member(archive(entries), name)

    def test_archive_and_member_selection_are_explicit(self):
        for source, member in ((archive((("a", b"x"),)), None), (b"not a ZIP", "a")):
            with self.subTest(member=member), self.assertRaises(InputRejected):
                select_member(source, member)

    def test_inspection_never_offers_a_nul_truncated_member_name(self):
        source = archive((("aXb", b"x"),)).replace(b"aXb", b"a\0b")
        with self.assertRaisesRegex(InputRejected, "NUL"):
            inspect_upload(source)
        with self.assertRaisesRegex(InputRejected, "NUL"):
            select_member(source, "a")

    def test_symlink_and_directory_are_not_regular_molecular_files(self):
        symlink = ZipInfo("link")
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        for name in (symlink, "directory/"):
            with self.subTest(name=name), self.assertRaisesRegex(InputRejected, "regular file"):
                select_member(archive(((name, b"x"),)), name.filename if isinstance(name, ZipInfo) else name)

    def test_declared_member_limit_is_checked_before_decoder_open(self):
        source = archive((("a", b"x" * (MAX_INPUT_BYTES + 1)),))
        with (
            patch.object(ZipFile, "open", side_effect=AssertionError("Oversize decoder was opened.")),
            self.assertRaisesRegex(InputRejected, "member limit"),
        ):
            select_member(source, "a")

    def test_crc_failure_does_not_return_a_partial_member(self):
        source = bytearray(archive((("a", b"XYZ_PAYLOAD"),)))
        source[source.index(b"XYZ_PAYLOAD")] ^= 1
        with self.assertRaisesRegex(InputRejected, "integrity"):
            select_member(bytes(source), "a")

    def test_truncated_zip_does_not_fall_back_to_plain_text(self):
        with self.assertRaisesRegex(InputRejected, "readable ZIP"):
            inspect_upload(b"PK\x03\x04truncated")

    def test_unsupported_extraction_version_is_correctable_and_keeps_private_reason(self):
        source = bytearray(archive((("a", b"x"),)))
        struct.pack_into("<H", source, source.index(b"PK\x01\x02") + 6, 1000)
        with self.assertRaisesRegex(InputRejected, "unsupported ZIP version") as caught:
            inspect_upload(bytes(source))
        self.assertIn("NotImplementedError", caught.exception.diagnostic)

    def test_entry_count_is_refused_before_central_directory_loading(self):
        source = bytearray(archive(()))
        struct.pack_into("<HH", source, 8, 4097, 4097)
        with (
            patch("nmrnet_provider.upload_archive.ZipFile", side_effect=AssertionError("Index was allocated.")),
            self.assertRaisesRegex(InputRejected, "4096"),
        ):
            inspect_upload(bytes(source))

    def test_member_listing_has_its_own_finite_model_handoff_bound(self):
        source = archive(tuple((str(n) + "x" * 200, b"x") for n in range(100)))
        with self.assertRaisesRegex(InputRejected, "listing"):
            inspect_upload(source)

    def test_encrypted_member_is_refused_before_decoder_open(self):
        source = bytearray(archive((("a", b"x"),)))
        struct.pack_into("<H", source, 6, 1)
        struct.pack_into("<H", source, source.index(b"PK\x01\x02") + 8, 1)
        with (
            patch.object(ZipFile, "open", side_effect=AssertionError("Encrypted decoder was opened.")),
            self.assertRaisesRegex(InputRejected, "encrypted"),
        ):
            select_member(bytes(source), "a")

    def test_actual_decoder_output_is_bounded_independently_of_declared_size(self):
        source = archive((("a", b"x"),))
        for payload, reason in ((b"x" * (MAX_INPUT_BYTES + 1), "member limit"), (b"", "declared size")):
            with (
                self.subTest(size=len(payload)),
                patch.object(ZipFile, "open", return_value=io.BytesIO(payload)),
                self.assertRaisesRegex(InputRejected, reason),
            ):
                select_member(source, "a")

    def test_native_errno_remains_operational_while_owned_decoder_corruption_is_correctable(self):
        source = archive((("a", b"x"),))
        for error in (OSError(errno.EIO, "PRIVATE_NATIVE_CAUSE"), OSError("PRIVATE_DECODER_CAUSE")):
            with self.subTest(errno=error.errno), patch.object(ZipFile, "open") as opener:
                opener.return_value.read.side_effect = error
                expected = OSError if error.errno is not None else InputRejected
                with self.assertRaises(expected) as caught:
                    select_member(source, "a")
                if expected is InputRejected:
                    self.assertIs(caught.exception.__cause__, error)
                    self.assertNotIn("PRIVATE_DECODER_CAUSE", str(caught.exception))
                else:
                    self.assertIs(caught.exception, error)

    def test_zip64_count_is_refused_before_index_allocation(self):
        record = bytearray(56)
        record[:4] = b"PK\x06\x06"
        struct.pack_into("<Q", record, 32, 4097)
        locator = b"PK\x06\x07" + struct.pack("<IQI", 0, 0, 1)
        eocd = bytearray(archive(()))
        struct.pack_into("<HH", eocd, 8, 65535, 65535)
        with (
            patch("nmrnet_provider.upload_archive.ZipFile", side_effect=AssertionError("Index was allocated.")),
            self.assertRaisesRegex(InputRejected, "4096"),
        ):
            inspect_upload(bytes(record) + locator + bytes(eocd))
