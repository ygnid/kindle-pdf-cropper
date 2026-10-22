"""CLI-level tests (end-to-end through argparse)."""

from __future__ import annotations

from conftest import run_cli


def test_cli_pdf_default(text_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    rc = run_cli([str(text_pdf), "-o", str(out), "--format", "pdf"])
    assert rc == 0
    assert out.exists()
    assert out.stat().st_size > 1000


def test_cli_epub_and_pdf(text_pdf, tmp_path):
    out_stem = tmp_path / "book"
    rc = run_cli(
        [str(text_pdf), "-o", str(out_stem), "--format", "both", "--split"]
    )
    assert rc == 0
    assert (tmp_path / "book.pdf").exists()
    assert (tmp_path / "book.epub").exists()


def test_cli_missing_file(tmp_path):
    rc = run_cli([str(tmp_path / "nope.pdf")])
    assert rc == 1


def test_cli_invalid_config(text_pdf, tmp_path):
    rc = run_cli(
        [
            str(text_pdf),
            "-o",
            str(tmp_path / "x"),
            "--split-overlap-ratio",
            "5",
        ]
    )
    assert rc == 1
