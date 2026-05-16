"""Shared fixtures for the AI accuracy eval suite.

Loads .env so GEMINI / ANTHROPIC / BRAVE keys are available, discovers fixture
directories under fixtures/, and exposes helpers that the per-layer test files
use to run the pipeline against a frozen proforma + web_context pair.

Design notes:

- This conftest is scoped to tests/ai_tests/ only. The parent tests/conftest.py
  is the one that hard-requires Postgres for the route/auth tests — we do NOT
  want that here, since the eval suite runs Gemini + Claude directly without
  touching the Flask DB.

- Brave Search is FROZEN per-fixture. The first time a fixture runs (or when
  freeze_fixtures.py is invoked), the real `gather_web_context_for_content`
  is called and the result saved to `fixtures/<name>/web_context.txt`. Every
  test after that uses the saved text — deterministic, free, and identical
  across runs.

- The pipeline helper here calls `generate_pis_data` + `classify_flat_pis_origins`
  directly. It deliberately skips the bulk-wizard task orchestration so each
  test exercises the units we actually care about (generation + tagging),
  not the wizard's parallel-task plumbing.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Make project root importable so we can pull in helpers / utils.
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Load .env BEFORE any utils import — they read os.environ at module level.
load_dotenv(_PROJECT_ROOT / ".env")

FIXTURES_DIR = _THIS_DIR / "fixtures"
ADVERSARIAL_DIR = _THIS_DIR / "adversarial"
RUNS_DIR = _THIS_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)


# ────────────────────────────────────────────────────────────────────────────
# Override the parent tests/conftest.py autouse fixtures that require Postgres.
# The eval suite calls Gemini + Claude directly — it never touches the Flask
# app or DB, so the parent's `_postgres_required` / `reset_db` should not run.
# These local fixtures shadow the parent's for any test collected from this
# directory.
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def _postgres_required():
    """No-op override — eval suite doesn't need Postgres."""
    yield


@pytest.fixture(autouse=True)
def reset_db():
    """No-op override — eval suite doesn't touch the DB."""
    yield


# ────────────────────────────────────────────────────────────────────────────
# Fixture data model
# ────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Fixture:
    name: str
    dir: Path
    proforma_path: Path
    metadata: dict
    web_context_path: Path

    @property
    def web_context(self) -> str:
        """Frozen Brave output for this fixture. Empty string if not yet captured."""
        if not self.web_context_path.exists():
            return ""
        return self.web_context_path.read_text(encoding="utf-8")

    @property
    def proforma_text(self) -> str:
        """Raw text extracted from the proforma — used by the grep layers."""
        from helpers import extract_raw_text_from_files
        return extract_raw_text_from_files([str(self.proforma_path)]) or ""

    @property
    def product_name(self) -> str:
        return self.metadata.get("product_name", "")

    @property
    def brand(self) -> str:
        return self.metadata.get("brand", "")


def _load_fixture(fixture_dir: Path) -> Fixture | None:
    meta_path = fixture_dir / "metadata.json"
    if not meta_path.exists():
        return None
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    proforma_name = metadata.get("input_file", "proforma.pdf")
    proforma_path = fixture_dir / proforma_name
    if not proforma_path.exists():
        return None
    return Fixture(
        name=fixture_dir.name,
        dir=fixture_dir,
        proforma_path=proforma_path,
        metadata=metadata,
        web_context_path=fixture_dir / "web_context.txt",
    )


def _discover_fixtures(root: Path) -> list[Fixture]:
    if not root.exists():
        return []
    out: list[Fixture] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        f = _load_fixture(d)
        if f is not None:
            out.append(f)
    return out


# ────────────────────────────────────────────────────────────────────────────
# Pytest fixtures
# ────────────────────────────────────────────────────────────────────────────

def pytest_collection_modifyitems(items):
    """Skip every test in this suite if ANTHROPIC_API_KEY is missing AND the
    test depends on the judge. Layer 1 (pure grep) and Layer 4 (consistency)
    can still run without Anthropic; the markers do the gating."""
    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY"))
    has_gemini = bool(os.getenv("GOOGLE_API_KEY"))
    skip_no_anthropic = pytest.mark.skip(reason="ANTHROPIC_API_KEY not set")
    skip_no_gemini = pytest.mark.skip(reason="GOOGLE_API_KEY not set")
    for item in items:
        if not has_gemini:
            item.add_marker(skip_no_gemini)
        elif not has_anthropic and "needs_anthropic" in item.keywords:
            item.add_marker(skip_no_anthropic)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "needs_anthropic: test requires ANTHROPIC_API_KEY (Layer 2, 3, 5 judge calls)",
    )


@pytest.fixture(scope="session")
def all_fixtures() -> list[Fixture]:
    """All real fixtures under tests/ai_tests/fixtures/."""
    return _discover_fixtures(FIXTURES_DIR)


@pytest.fixture(scope="session")
def adversarial_fixtures() -> list[Fixture]:
    """The synthetic trap fixtures (Layer 5). May be empty until generated."""
    return _discover_fixtures(ADVERSARIAL_DIR)


def pytest_generate_tests(metafunc):
    """Parametrize any test that takes a `fixture` argument over every
    discovered real fixture. Keeps the per-layer test files terse."""
    if "fixture" in metafunc.fixturenames and "adversarial" not in metafunc.function.__name__:
        fxs = _discover_fixtures(FIXTURES_DIR)
        metafunc.parametrize("fixture", fxs, ids=[f.name for f in fxs])
    if "adversarial" in metafunc.fixturenames:
        fxs = _discover_fixtures(ADVERSARIAL_DIR)
        metafunc.parametrize("adversarial", fxs, ids=[f.name for f in fxs])


# ────────────────────────────────────────────────────────────────────────────
# Pipeline runner — what every layer test calls to generate pis_data
# ────────────────────────────────────────────────────────────────────────────

def run_pipeline(
    fixture: Fixture,
    use_cache: bool = True,
    force_refresh: bool = False,
    force_refresh_web: bool = False,
    cache_filename: str = "pis_data.json",
) -> dict:
    """Run generate_pis_data on a fixture and return pis_data with origin tags.

    Two-stage caching keeps this $0 to re-run after first invocation:

    - **web_context.txt** is captured from Brave on first call, then frozen.
    - **<cache_filename>** is captured from Gemini on first call, then frozen.

    Set `force_refresh=True` to re-run Gemini (e.g. after a prompt change).
    Set `force_refresh_web=True` to also re-hit Brave.
    Set `use_cache=False` to bypass disk cache entirely.
    Set `cache_filename="pis_data_run2.json"` to keep multiple cached runs
    of the same fixture side-by-side (used by Layer 4 consistency).

    Returns the pis_data dict with `_field_origins`, `_spec_origins`, and
    `_web_context` populated.
    """
    cache_path = fixture.dir / cache_filename

    # ── Fast path: read cached pis_data if available ──
    if use_cache and not force_refresh and cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(cached, dict) and cached.get("_field_origins") is not None:
            return cached

    from utils.ai_generation import generate_pis_data
    from helpers import classify_flat_pis_origins

    # ── Stage 1: ensure web_context is frozen on disk ──
    web_context = fixture.web_context
    if force_refresh_web or not web_context:
        web_context = _freeze_web_context(fixture)

    url_data = {"text": web_context, "html": ""}

    # ── Stage 2: call the generator (Gemini) ──
    pis_data = generate_pis_data(
        [str(fixture.proforma_path)],
        fixture.product_name,
        url_data,
    ) or {}

    # ── Stage 3: tag origins ──
    pis_data["_web_context"] = web_context
    field_origins, spec_origins = classify_flat_pis_origins(
        pis_data,
        fixture.proforma_text,
        web_context=web_context,
    )
    pis_data["_field_origins"] = field_origins
    pis_data["_spec_origins"] = spec_origins

    # ── Stage 4: persist to disk so the next run is free ──
    if use_cache:
        cache_path.write_text(
            json.dumps(pis_data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    return pis_data


def walk_pis_field(pis_data: dict, dotted_path: str):
    """Resolve a dotted path like 'header_info.product_name' against pis_data.
    Returns the value or None if any segment is missing."""
    cursor: object = pis_data
    for key in dotted_path.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(key)
        if cursor is None:
            return None
    return cursor


def _freeze_web_context(fixture: Fixture) -> str:
    """Run the real Brave-discovery pipeline once and save the result."""
    from utils.image_processing import gather_web_context_for_content
    try:
        text = gather_web_context_for_content(
            fixture.product_name,
            brand=fixture.brand or None,
        ) or ""
    except Exception as e:
        print(f"  [freeze] Brave call failed for {fixture.name}: {e}")
        text = ""
    fixture.web_context_path.write_text(text, encoding="utf-8")
    return text
