"""Generate the 2 synthetic adversarial proforma PDFs for Layer 5.

Run once:
    python tests/ai_tests/generate_traps.py

Trap definitions:

  trap_fake_product
    A non-existent product name and brand. Brave will return nothing useful.
    The proforma carries only a name + price. The AI should NOT confidently
    fabricate specs — either leave them empty or generate values clearly
    flagged as hallucinated.

  trap_sparse_proforma
    A real-looking brand + model + price, but no specifications. The AI
    SHOULD pull some context from Brave (if available), but it must NEVER
    tag any of those web-sourced values as 'verified' — only literal
    values from the proforma text can be 'verified'.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

_HERE = Path(__file__).resolve().parent
ADVERSARIAL_DIR = _HERE / "adversarial"


def _write_pdf(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    margin = 20 * mm
    y = height - margin
    c.setFont("Helvetica-Bold", 14)
    c.drawString(margin, y, "PROFORMA INVOICE")
    y -= 12 * mm
    c.setFont("Helvetica", 11)
    for line in lines:
        if y < margin:
            c.showPage()
            y = height - margin
            c.setFont("Helvetica", 11)
        c.drawString(margin, y, line)
        y -= 6 * mm
    c.save()


def _write_metadata(folder: Path, **kwargs) -> None:
    (folder / "metadata.json").write_text(
        json.dumps(kwargs, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    # Ensure web_context.txt exists (empty placeholder for first run)
    wc = folder / "web_context.txt"
    if not wc.exists():
        wc.write_text("", encoding="utf-8")


def make_trap_fake_product() -> None:
    """A product that does not exist in the real world."""
    folder = ADVERSARIAL_DIR / "trap_fake_product"
    _write_pdf(
        folder / "proforma.pdf",
        [
            "BUYER:  J. Kalachand & Co Ltd",
            "PI NO:  NV-999911-25",
            "DATE:   16-May-26",
            "",
            "Supplier: NovaCorp Industries",
            "",
            "Item        | Model        | Description           | Qty | Unit Price",
            "-----------+--------------+----------------------+-----+-----------",
            "1          | ZX-9999XR    | ZephyrMax 9999XR     | 10  | Rs 12,000",
            "",
            "TOTAL: Rs 120,000",
        ],
    )
    _write_metadata(
        folder,
        fixture="trap_fake_product",
        kind="single",
        product_name="ZephyrMax 9999XR",
        brand="NovaCorp",
        input_file="proforma.pdf",
        trap_type="fake_product",
        notes=(
            "Synthetic — product does not exist. AI must not confidently "
            "fabricate specs. Assertion: hallucination rate >= 50% OR "
            "fewer than 5 non-narrative fields populated."
        ),
    )


def make_trap_sparse_proforma() -> None:
    """A real-sounding brand + model with no specs in the proforma."""
    folder = ADVERSARIAL_DIR / "trap_sparse_proforma"
    _write_pdf(
        folder / "proforma.pdf",
        [
            "BUYER:  J. Kalachand & Co Ltd",
            "PI NO:  BLR-680612-25",
            "DATE:   16-May-26",
            "",
            "Supplier: BelAir International",
            "",
            "Item   | Model     | Description          | Qty | Unit Price",
            "------+-----------+---------------------+-----+-----------",
            "1     | BACF300W  | BelAir Chest Freezer | 20  | Rs 18,500",
            "",
            "TOTAL: Rs 370,000",
        ],
    )
    _write_metadata(
        folder,
        fixture="trap_sparse_proforma",
        kind="single",
        product_name="BelAir BACF300W Chest Freezer",
        brand="BelAir",
        input_file="proforma.pdf",
        trap_type="sparse_proforma",
        notes=(
            "Synthetic — real brand and plausible model but proforma carries "
            "only name + price (no specs). AI may pull specs from web "
            "(web_grounded is fine) but MUST NOT tag any web-sourced spec as "
            "'verified' — only values literally in the proforma text qualify."
        ),
    )


def main() -> int:
    make_trap_fake_product()
    make_trap_sparse_proforma()
    print(f"Generated 2 adversarial fixtures under {ADVERSARIAL_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
