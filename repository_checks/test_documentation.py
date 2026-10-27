"""Keep published HTML navigable and source quotations faithful to their citations."""

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
        # Pages use HTML; the API offering is a packaged Markdown input.
        # The remaining exceptions are the README, data and a plain-text design record.
        exceptions = {"README.md", "nmrnet_provider/about/hello.md",
                      "containers/inference/image-inputs.txt", "tests/fixtures/small_chloromethanes.txt",
                      "docs/notes/022_shared_delivery_deferred_20261024.txt"}
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

    def test_record_quotations_match_their_cited_source_lines(self):
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
                # Notes retain evidence excerpts, not operational transcripts.
                # Original line IDs keep citations stable across omissions.
                source_lines = original.decode().splitlines(keepends=True)
                retained = [(int(identity[1:]), "".join(text)) for identity, text in page.lines]
                self.assertTrue(retained, "Retain the cited design and evidence quotations.")
                indices = [index for index, _ in retained]
                self.assertEqual(indices, sorted(set(indices)), "Keep original source order and unique line IDs.")
                for index, text in retained:
                    self.assertGreaterEqual(index, 1)
                    self.assertLessEqual(index, len(source_lines))
                    # A quotation may omit leading coordination metadata, but
                    # the remaining wording must be an exact source suffix.
                    self.assertTrue(source_lines[index - 1].endswith(text), (path, index, text))
                self.assertEqual("".join(page.record), "".join(text for _, text in retained))
                records += 1
        self.assertGreaterEqual(records, 13, "Historical notes must retain their cited source evidence.")
