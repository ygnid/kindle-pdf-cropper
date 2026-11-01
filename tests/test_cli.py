"""CLI-level tests (end-to-end through argparse)."""

from __future__ import annotations

import pymupdf as fitz
import pytest

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


def test_cli_split_max_aspect_ratio(text_pdf, tmp_path):
    out = tmp_path / "wide.pdf"
    rc = run_cli(
        [
            str(text_pdf),
            "--split",
            "--max-aspect-ratio",
            "0.1",
            "-o",
            str(out),
        ]
    )
    assert rc == 0
    doc = fitz.open(str(out))
    try:
        assert len(doc) == 10
        for page in doc:
            assert page.rect.height / page.rect.width <= 0.1 + 1e-6
    finally:
        doc.close()


def test_cli_split_device_type(text_pdf, tmp_path):
    out = tmp_path / "kindle.pdf"
    rc = run_cli(
        [
            str(text_pdf),
            "--split",
            "--device-type",
            "paperwhite-5",
            "-o",
            str(out),
        ]
    )
    assert rc == 0
    doc = fitz.open(str(out))
    try:
        for page in doc:
            assert page.rect.height / page.rect.width <= 1236 / 1648 + 1e-6
    finally:
        doc.close()


def test_cli_aspect_ratio_options_are_mutually_exclusive(text_pdf, tmp_path):
    with pytest.raises(SystemExit):
        run_cli(
            [
                str(text_pdf),
                "--split",
                "--max-aspect-ratio",
                "0.75",
                "--device-type",
                "kindle",
                "-o",
                str(tmp_path / "x"),
            ]
        )


def test_cli_default_output_name_uses_cropped_and_split(text_pdf):
    assert run_cli([str(text_pdf)]) == 0
    assert (text_pdf.parent / (text_pdf.stem + "_cropped.pdf")).exists()

    assert run_cli([str(text_pdf), "--split"]) == 0
    assert (text_pdf.parent / (text_pdf.stem + "_split.pdf")).exists()


def test_cli_prompts_for_device_when_interactive(
    text_pdf, tmp_path, monkeypatch
):
    import kindle_pdf_cropper.cli as cli

    monkeypatch.setattr(cli, "_tui_available", lambda: True)
    monkeypatch.setattr(
        cli, "_prompt_split_device", lambda: ("paperwhite-5", None)
    )
    out = tmp_path / "menu.pdf"
    rc = cli.main([str(text_pdf), "--split", "-o", str(out)])
    assert rc == 0
    doc = fitz.open(str(out))
    try:
        for page in doc:
            assert page.rect.height / page.rect.width <= 1236 / 1648 + 1e-6
    finally:
        doc.close()


def test_device_menu_items_include_no_limit_and_models():
    import kindle_pdf_cropper.cli as cli

    values = [value for _, value in cli._device_menu_items()]
    assert None in values
    assert "manual" in values
    assert "kindle" in values
    assert "paperwhite-5" in values


def test_prompt_split_device_selects_with_arrows(monkeypatch):
    import kindle_pdf_cropper.cli as cli

    menu = [
        ("No limit", None),
        ("Manual", "manual"),
        ("Paperwhite 5", "paperwhite-5"),
    ]
    monkeypatch.setattr(cli, "_device_menu_items", lambda: menu)
    monkeypatch.setattr(cli, "_enable_ansi", lambda: None)
    keys = iter(["down", "down", "enter"])
    monkeypatch.setattr(cli, "_read_key", lambda: next(keys))
    assert cli._prompt_split_device() == ("paperwhite-5", None)


def test_prompt_split_device_escape_means_no_limit(monkeypatch):
    import kindle_pdf_cropper.cli as cli

    monkeypatch.setattr(
        cli, "_device_menu_items",
        lambda: [("No limit", None), ("Manual", "manual")],
    )
    monkeypatch.setattr(cli, "_enable_ansi", lambda: None)
    monkeypatch.setattr(cli, "_read_key", lambda: "escape")
    assert cli._prompt_split_device() == (None, None)
