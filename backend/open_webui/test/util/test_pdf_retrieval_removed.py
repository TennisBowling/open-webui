import pytest

from open_webui.retrieval.loaders.main import (
    PDF_RETRIEVAL_REMOVED_MESSAGE,
    Loader,
    is_pdf_file,
)


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("report.pdf", "application/octet-stream"),
        ("REPORT.PDF", ""),
        ("report", "application/pdf"),
        ("report.bin", "application/pdf; charset=binary"),
    ],
)
def test_is_pdf_file_recognizes_extension_and_content_type(filename, content_type):
    assert is_pdf_file(filename, content_type)


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("report.docx", "application/octet-stream"),
        ("report.pdf.txt", "text/plain"),
        ("", ""),
    ],
)
def test_is_pdf_file_ignores_non_pdf_files(filename, content_type):
    assert not is_pdf_file(filename, content_type)


@pytest.mark.parametrize(
    "engine",
    [
        "",
        "external",
        "tika",
        "datalab_marker",
        "docling",
        "document_intelligence",
        "mineru",
    ],
)
def test_loader_rejects_pdf_before_dispatching_to_any_engine(engine):
    with pytest.raises(
        ValueError, match="PDF retrieval parsing has been removed"
    ) as exc:
        Loader(engine=engine).load(
            filename="report.pdf",
            file_content_type="application/pdf",
            file_path="/path/that/must/not/be-read",
        )

    assert str(exc.value) == PDF_RETRIEVAL_REMOVED_MESSAGE
