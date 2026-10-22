"""Shared fixtures: synthetic PDFs for the test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from make_samples import (  # noqa: E402
    make_scanned_pdf,
    make_text_pdf,
    make_tight_scan_pdf,
)


@pytest.fixture()
def text_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "text.pdf"
    make_text_pdf(str(p), pages=5)
    return p


@pytest.fixture()
def scan_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "scan.pdf"
    make_scanned_pdf(str(p), pages=3, rotate=0)
    return p


@pytest.fixture()
def scan_rot_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "scan_rot.pdf"
    make_scanned_pdf(str(p), pages=3, rotate=90)
    return p


@pytest.fixture()
def tight_scan_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "tight_scan.pdf"
    make_tight_scan_pdf(str(p), pages=4)
    return p


def run_cli(argv: list[str]) -> int:
    from kindle_pdf_cropper.cli import main

    return main(argv)
