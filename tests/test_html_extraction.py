from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from corpus.vendor.context_forge_extractors import _HTMLText, extract


def _parse(value: str, base_url: str | None = None) -> _HTMLText:
    parser = _HTMLText(base_url)
    parser.feed(value)
    parser.close()
    parser.text()
    return parser


class HtmlExtractionTests(unittest.TestCase):
    def test_suppresses_non_visible_payloads_and_captures_title(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "sample.html"
            path.write_text(
                "<head><title>A &amp; B</title><style>.secret{x:y}</style></head>"
                "<p>Visible &amp; decoded</p><script>hidden()</script>"
                "<template><noscript>also hidden</noscript></template>",
                encoding="utf-8",
            )
            result = extract(path)
        self.assertIn("Visible & decoded", result.sections[0].text)
        for value in ("A & B", ".secret", "hidden", "also hidden"):
            self.assertNotIn(value, result.sections[0].text)
        self.assertEqual(result.metadata["html_title"], "A & B")

    def test_preserves_safe_links_without_inventing_a_local_origin(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "links.html"
            path.write_text(
                '<a href="https://example.test/a">Example</a> '
                '<a href="relative/page">Relative</a> '
                '<a href="mailto:a@example.test">Mail</a> '
                '<a href="https://example.test/a">https://example.test/a</a>'
                '<a href="https://example.test/empty"></a>',
                encoding="utf-8",
            )
            text = extract(path).sections[0].text
        self.assertIn("[Example](https://example.test/a)", text)
        self.assertIn("[Relative](relative/page)", text)
        self.assertIn("[Mail](mailto:a@example.test)", text)
        self.assertNotIn("[https://example.test/a](https://example.test/a)", text)
        self.assertNotIn("https://example.test/empty", text)
        self.assertTrue(text.endswith("https://example.test/a"))

    def test_omits_dangerous_targets_but_keeps_visible_text(self):
        parser = _parse(
            '<a href="javascript:alert(1)">JS</a>'
            '<a href="data:text/plain,x">Data</a>'
            '<a href="vbscript:x">VB</a>'
        )
        self.assertEqual(parser.text(), "JSDataVB")
        self.assertEqual(parser.links, [])

    def test_trustworthy_base_url_resolves_relative_links(self):
        parser = _parse('<a href="child">Child</a>', "https://example.test/root/")
        self.assertEqual(parser.text(), "[Child](https://example.test/root/child)")

    def test_malformed_html_is_retained_conservatively_and_flagged(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "malformed.html"
            path.write_text("<p>Before<a href='relative'>linked", encoding="utf-8")
            result = extract(path)
        self.assertIn("Beforelinked", result.sections[0].text)
        self.assertIs(result.metadata["html_ambiguous_reading_order"], True)
        self.assertTrue(any("ambiguous" in warning for warning in result.warnings))

    def test_plain_text_extraction_is_unchanged(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "sample.txt"
            original = "literal <script> text &amp; stays literal\n"
            path.write_bytes(original.encode("utf-8"))
            result = extract(path)
        self.assertEqual(result.sections[0].text, original)
        self.assertEqual(result.metadata, {"encoding": "utf-8-sig"})


if __name__ == "__main__":
    unittest.main()

