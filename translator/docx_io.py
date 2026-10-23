"""Reading text out of a .docx file and writing translations back in.

A .docx file is a zip of XML files. The text lives in paragraphs (<w:p>), and each
paragraph is split into "runs" (<w:r>): pieces of text that share the same formatting.
"Hello **world**" is one paragraph with two runs: "Hello " and a bold "world".

We translate in place - the original document keeps its structure (headings, tables,
headers, footers, images) and only the words change.
"""
import re

from docx import Document
from docx.document import Document as DocumentType
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def open_document(path: str) -> DocumentType:
    return Document(path)


def _paragraphs_in(container):
    """Paragraphs in the body of a container (document, table cell, header), tables included."""
    yield from container.paragraphs
    for table in container.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from _paragraphs_in(cell)  # tables can contain tables


def iter_paragraphs(doc: DocumentType):
    """Every paragraph in the document exactly once: body, tables, headers and footers.

    Merged table cells are returned several times by python-docx (once per grid
    position), so we remember the underlying XML elements we've already seen.
    A set gives O(1) membership checks, so the whole walk is O(n).
    """
    seen = set()
    parts = [doc]
    for section in doc.sections:
        for part in (section.header, section.footer,
                     section.first_page_header, section.first_page_footer):
            if not part.is_linked_to_previous:  # linked = same as previous section's
                parts.append(part)
    for part in parts:
        for p in _paragraphs_in(part):
            if p._p not in seen:
                seen.add(p._p)
                yield p


def normalize(text: str) -> str:
    """Collapse line breaks, tabs and repeated spaces into single spaces."""
    return " ".join(text.split())


def needs_translation(text: str) -> bool:
    """Skip empty lines and paragraphs that are only numbers, codes or Latin text."""
    return bool(CYRILLIC.search(text))


def set_paragraph_text(p: Paragraph, text: str) -> None:
    """Replace a paragraph's text while keeping its style and images.

    The new text goes into the first run that has text (so it takes that run's
    formatting); the text of every later run is removed. Runs without text
    (images, page breaks) are left alone.

    Limitation: if a paragraph mixes formatting (one bold word), the whole
    translation takes the first run's formatting - word order changes between
    languages, so there's no reliable way to know where the bold word went.
    """
    runs = [r for r in p._p.xpath("./w:r | ./w:hyperlink/w:r") if r.xpath("./w:t")]
    if not runs:
        return
    first_texts = runs[0].xpath("./w:t")
    first_texts[0].text = text
    first_texts[0].set(qn("xml:space"), "preserve")  # keep leading/trailing spaces
    for t in first_texts[1:]:
        runs[0].remove(t)
    for r in runs[1:]:
        for t in r.xpath("./w:t"):
            r.remove(t)