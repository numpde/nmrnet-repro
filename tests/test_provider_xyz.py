"""Keep source frame identities and unit evidence through plain XYZ preparation."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import unittest

from nmrnet_provider import input_geometry, xyz

TWO_FRAMES = b"2\nfirst\nC 0 0 0\nH 0.109 0 0\n1\nsecond\nCl 0 0 0\n"


class XYZPreparationTests(unittest.TestCase):
    def test_all_frames_preserve_different_molecules_and_original_lines(self):
        frames = xyz.prepare_xyz(TWO_FRAMES, source_unit="nm", selection="all")
        self.assertEqual(2, len(frames))
        self.assertEqual((1, 2), tuple(frame.source_record_number for frame in frames))
        self.assertEqual((3, 4), frames[0].source_positions)
        self.assertEqual((7,), frames[1].source_positions)
        self.assertEqual(hashlib.sha256(TWO_FRAMES).hexdigest(), frames[1].source_sha256)
        self.assertEqual(["Cl"], json.loads(frames[1].request_bytes)["atoms"])
        self.assertAlmostEqual(1.09, json.loads(frames[0].request_bytes)["coordinates"][1][0], places=12)

    def test_explicit_later_frame_keeps_its_identity_without_first_frame_prediction(self):
        frames = xyz.prepare_xyz(TWO_FRAMES, source_unit="angstrom", selection=(2,))
        self.assertEqual(1, len(frames))
        self.assertEqual(2, frames[0].source_record_number)

    def test_explicit_order_is_preserved_without_sorting_sources(self):
        frames = xyz.prepare_xyz(TWO_FRAMES, source_unit="angstrom", selection=(2, 1))
        self.assertEqual((2, 1), tuple(frame.source_record_number for frame in frames))
        self.assertEqual((7,), frames[0].source_positions)

    def test_skipped_preceding_coordinates_do_not_change_later_record_identity(self):
        source = TWO_FRAMES.replace(b"C 0 0 0", b"invalid unselected atom row")
        frames = xyz.prepare_xyz(source, source_unit="angstrom", selection=(2,))
        self.assertEqual(2, frames[0].source_record_number)
        self.assertEqual((7,), frames[0].source_positions)

    def test_nonfinite_and_conversion_overflow_reject_the_entire_selection(self):
        for value in (b"nan", b"inf", b"1e308"):
            source = TWO_FRAMES.replace(b"Cl 0 0 0", b"Cl " + value + b" 0 0")
            with self.subTest(value=value), self.assertRaises(input_geometry.InputRejected):
                xyz.prepare_xyz(source, source_unit="nm", selection="all")

    def test_plaintext_xyz_must_contain_exactly_one_frame(self):
        with self.assertRaisesRegex(input_geometry.InputRejected, "one XYZ frame"):
            xyz.prepare_pasted_xyz(TWO_FRAMES, source_unit="nm")
        prepared = xyz.prepare_pasted_xyz(b"1\ncomment\nC 0 0 0\n", source_unit="angstrom")
        self.assertEqual(1, prepared.source_record_number)

    def test_bad_selected_record_names_its_original_number_and_never_returns_partial(self):
        source = TWO_FRAMES.replace(b"Cl 0 0 0", b"Cl private-bad-number 0 0")
        with self.assertRaisesRegex(input_geometry.InputRejected, "record 2") as caught:
            xyz.prepare_xyz(source, source_unit="angstrom", selection="all")
        self.assertNotIn("private-bad-number", str(caught.exception))
        self.assertEqual(1, len(xyz.prepare_xyz(source, source_unit="angstrom", selection=(1,))))

    def test_unsupported_or_ambiguous_selection_is_not_silently_adjusted(self):
        for selection in ((), (0,), (3,), (1, 1), (True,), "first", None):
            with self.subTest(selection=selection), self.assertRaises(input_geometry.InputRejected):
                xyz.prepare_xyz(TWO_FRAMES, source_unit="angstrom", selection=selection)

    def test_frame_boundaries_are_strict_and_dialect_errors_do_not_echo_comments(self):
        sources = (
            b"2\ncomment\nC 0 0 0\n",
            b"1\ncomment\nC 0 0 0\ntrailing-data\n",
            b"1\nProperties=species:S:1:pos:R:3 private-comment\nC 0 0 0\n",
            b"1\ncomment\n6 0 0 0\n",
            b"0\ncomment\n",
        )
        for source in sources:
            with self.subTest(source=source), self.assertRaises(input_geometry.InputRejected) as caught:
                xyz.prepare_xyz(source, source_unit="angstrom", selection="all")
            self.assertNotIn("private-comment", str(caught.exception))

    def test_bounded_record_count_rejects_before_constructing_unbounded_output(self):
        accepted = xyz.prepare_xyz(b"1\ncomment\nC 0 0 0\n" * 128, source_unit="angstrom", selection=(128,))
        self.assertEqual(128, accepted[0].source_record_number)
        self.assertEqual((384,), accepted[0].source_positions)
        source = b"1\ncomment\nC 0 0 0\n" * 129
        with self.assertRaisesRegex(input_geometry.InputRejected, "record limit"):
            xyz.prepare_xyz(source, source_unit="angstrom", selection="all")

    def test_bohr_conversion_records_the_codata_reference(self):
        prepared = input_geometry.prepare_table(b"H 2 0 0\nC 0 0 0\n", source_unit="bohr")
        self.assertAlmostEqual(1.058354421088, json.loads(prepared.request_bytes)["coordinates"][0][0], places=12)
        self.assertEqual("CODATA 2022", prepared.conversion_reference)
        self.assertEqual(0.529177210544, prepared.scale_to_angstrom)
