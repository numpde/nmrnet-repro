"""Keep published HTML navigable and dated records faithful to their cited source."""

from tools.container_guard import require_container

require_container()

import hashlib
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from repository_checks.quality.sources import ROOT


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.links = []
        self.metadata = {}
        self.record = []
        self.in_record = False
        self.lines = []
        self.current_line = None
        self.stylesheets = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        for name in ("href", "src"):
            if name in attrs:
                self.links.append(attrs[name])
        if tag == "meta" and "name" in attrs:
            self.metadata[attrs["name"]] = attrs.get("content", "")
        if tag == "pre" and attrs.get("id") == "record-text":
            self.in_record = True
        if self.in_record and tag == "span" and attrs.get("id", "").startswith("L"):
            self.current_line = []
            self.lines.append((attrs["id"], self.current_line))
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.stylesheets.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "pre":
            self.in_record = False
        if tag == "span":
            self.current_line = None

    def handle_data(self, data):
        if self.in_record:
            self.record.append(data)
        if self.current_line is not None:
            self.current_line.append(data)


class DocumentationTests(unittest.TestCase):
    def test_owned_prose_has_one_html_home(self):
        paths = (
            subprocess.run(
                ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                cwd=ROOT,
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .split("\0")
        )
        # The README is the repository entry point; the two .txt files are data.
        exceptions = {"README.md", "containers/inference/image-inputs.txt", "tests/fixtures/small_chloromethanes.txt"}
        leftovers = [
            name
            for name in paths
            if name
            and (ROOT / name).is_file()
            and Path(name).suffix in {".md", ".txt"}
            and name not in exceptions
            and not name.startswith("NMRNetplusplus/")
        ]
        self.assertEqual([], leftovers, "Move owned prose to docs/*.html; keep one maintained source.")

    def test_pages_have_unique_anchors_and_working_local_links(self):
        root = ROOT / "docs"
        pages = {path.resolve(): Page(path.read_text()) for path in root.rglob("*.html")}
        self.assertIn(root / "index.html", pages)
        for path, page in pages.items():
            with self.subTest(page=path.relative_to(root)):
                self.assertEqual(len(page.ids), len(set(page.ids)), "Duplicate fragment IDs")
                self.assertIn("main", page.ids)
                self.assertTrue(page.stylesheets, "Each page needs its local stylesheet.")
                for stylesheet in page.stylesheets:
                    self.assertFalse(urlsplit(stylesheet).scheme or urlsplit(stylesheet).netloc, stylesheet)
                for link in page.links:
                    parts = urlsplit(link)
                    if parts.scheme or parts.netloc:
                        continue
                    target = (path.parent / unquote(parts.path)).resolve() if parts.path else path
                    if target.is_dir():
                        target /= "index.html"
                    self.assertTrue(target.is_relative_to(root), (path, link))
                    self.assertTrue(target.is_file(), (path, link))
                    if parts.fragment:
                        self.assertIn(unquote(parts.fragment), pages[target].ids, (path, link))

    def test_literal_records_match_their_original_git_bytes(self):
        records = 0
        for path in (ROOT / "docs").rglob("*.html"):
            page = Page(path.read_text())
            if "record-text" not in page.ids:
                continue
            with self.subTest(record=path.relative_to(ROOT)):
                revision = page.metadata["original-source-revision"]
                source = page.metadata["original-source-path"]
                self.assertRegex(revision, r"^[0-9a-f]{40}$")
                original = subprocess.run(
                    ["git", "show", f"{revision}:{source}"],
                    cwd=ROOT,
                    capture_output=True,
                    check=True,
                ).stdout
                self.assertEqual(hashlib.sha256(original).hexdigest(), page.metadata["original-source-sha256"])
                self.assertEqual(original, "".join(page.record).encode())
                self.assertEqual(
                    [(f"L{index}", line) for index, line in enumerate(original.decode().splitlines(keepends=True), 1)],
                    [(identity, "".join(text)) for identity, text in page.lines],
                )
                records += 1
        self.assertGreaterEqual(records, 13, "Historical notes must remain available, with literal source evidence.")
