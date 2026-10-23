"""GUI packaging preserves the molecular file's exact bytes and logical name."""

import io
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from nmrnet_provider.upload_archive import inspect_upload, select_member


def archive(entries):
    stream = io.BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as writer:
        for name, source in entries:
            writer.writestr(name, source)
    return stream.getvalue()


class UploadArchiveTests(unittest.TestCase):
    def test_direct_source_is_identified_without_rewriting_bytes(self):
        source = b"C 0 0 0\nH 1.09 0 0\n"
        self.assertEqual({"container": "direct", "members": []}, inspect_upload(source))

    def test_single_file_archive_keeps_exact_member_bytes(self):
        source = b"C 0 0 0\nH 1.09 0 0\n"
        zipped = archive((("structures/example.xyz", source),))
        self.assertEqual(
            {"container": "zip", "members": [{"name": "structures/example.xyz", "byte_length": len(source)}]},
            inspect_upload(zipped),
        )
        self.assertEqual(source, select_member(zipped, "structures/example.xyz"))

    def test_member_choice_is_exact_and_independent_of_archive_order(self):
        entries = (("../first.xyz", b"first"), ("/second.xyz", b"second"))
        for ordered in (entries, tuple(reversed(entries))):
            self.assertEqual(b"second", select_member(archive(ordered), "/second.xyz"))
