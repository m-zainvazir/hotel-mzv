"""app/rag/extract.py — each extension, an unknown extension is a clean
error, HTML stripping drops scripts and styles (Phase 9 Part C)."""

from __future__ import annotations

import io

import pytest

from app.rag.extract import ExtractionError, extract_text, html_to_text


def test_txt_extension():
    assert extract_text("notes.txt", b"hello world") == "hello world"


def test_md_extension():
    assert extract_text("readme.md", b"# Title\n\nBody text.") == "# Title\n\nBody text."


def test_csv_extension():
    data = b"name,age\r\nAlice,30\r\nBob,25\r\n"
    result = extract_text("data.csv", data)
    assert "name, age" in result
    assert "Alice, 30" in result
    assert "Bob, 25" in result


def test_extension_matching_is_case_insensitive():
    assert extract_text("NOTES.TXT", b"hello") == "hello"


def test_pdf_extension_via_a_generated_pdf():
    pypdf = pytest.importorskip("pypdf")

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)

    # A blank page has no text — this proves the PDF path runs end to end
    # without raising, not that it extracts specific content.
    text = extract_text("file.pdf", buf.getvalue())
    assert isinstance(text, str)


def test_malformed_pdf_raises_extraction_error_not_a_bare_exception():
    pytest.importorskip("pypdf")
    with pytest.raises(ExtractionError):
        extract_text("bad.pdf", b"this is not a real pdf file at all")


def test_docx_extension():
    docx = pytest.importorskip("docx")

    document = docx.Document()
    document.add_paragraph("Hello from docx.")
    document.add_paragraph("A second paragraph.")
    buf = io.BytesIO()
    document.save(buf)

    text = extract_text("file.docx", buf.getvalue())
    assert "Hello from docx." in text
    assert "A second paragraph." in text


def test_malformed_docx_raises_extraction_error_not_a_bare_exception():
    pytest.importorskip("docx")
    with pytest.raises(ExtractionError):
        extract_text("bad.docx", b"this is not a real docx file at all")


def test_html_extension_strips_scripts_and_styles():
    html = b"""
    <html><head><style>body { color: red; }</style></head>
    <body><script>alert('should not appear')</script><p>Real content here.</p></body></html>
    """
    text = extract_text("page.html", html)
    assert "Real content here." in text
    assert "color: red" not in text
    assert "should not appear" not in text


def test_htm_extension_also_works():
    text = extract_text("page.htm", b"<p>Some text</p>")
    assert "Some text" in text


def test_html_to_text_directly():
    result = html_to_text("<p>Hello <b>world</b></p><script>evil()</script>")
    assert "Hello" in result
    assert "world" in result
    assert "evil" not in result


def test_unknown_extension_is_a_clean_error():
    with pytest.raises(ExtractionError):
        extract_text("file.xyz", b"whatever")


def test_no_extension_is_a_clean_error():
    with pytest.raises(ExtractionError):
        extract_text("noextension", b"whatever")


def test_extraction_error_message_names_the_extension():
    with pytest.raises(ExtractionError, match=r"\.xyz"):
        extract_text("file.xyz", b"whatever")
