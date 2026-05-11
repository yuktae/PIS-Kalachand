"""
Workflow and helper unit tests.

Covers:
- Product model stage defaults and soft-delete
- save_version_snapshot: major vs minor, version numbering
- _compute_shallow_diff: detects changes, ignores identical values
- normalize_pis_data: fills in all missing keys
- validate_pis_data / validate_spec_data: warns on bad structure
"""
import os
import pytest
import urllib.parse
import psycopg2  # type: ignore[import-untyped]
from dotenv import load_dotenv


def _postgres_available():
    try:
        load_dotenv()
        url = os.environ.get('DATABASE_URL', 'postgresql://postgres:postgres@localhost:5432/pis_system')
        p = urllib.parse.urlparse(url)
        conn = psycopg2.connect(
            host=p.hostname, port=p.port or 5432,
            database=p.path.lstrip('/'),
            user=p.username, password=p.password,
            connect_timeout=2,
        )
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_available(),
    reason='PostgreSQL not reachable — skipping DB tests',
)


# ── Product model ─────────────────────────────────────────────────────────────

def test_product_default_stage_is_marketing_draft(app, sample_product):
    from model import db, Product
    with app.app_context():
        p = db.session.get(Product, sample_product)
        assert p is not None
        assert p.workflow_stage == 'marketing_draft'


def test_product_soft_delete_leaves_record(app, sample_product):
    from model import db, Product
    from datetime import datetime, timezone
    with app.app_context():
        p = db.session.get(Product, sample_product)
        assert p is not None
        p.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()
        # Record still exists in DB
        found = db.session.get(Product, sample_product)
        assert found is not None
        assert found.deleted_at is not None
        # Active query excludes it
        active = Product.query.filter(Product.deleted_at.is_(None),
                                      Product.id == sample_product).first()
        assert active is None
        # Restore for cleanup
        found.deleted_at = None
        db.session.commit()


# ── save_version_snapshot ─────────────────────────────────────────────────────

def test_first_snapshot_is_major(app, sample_product):
    from model import db, Product, ProductVersion
    from helpers import save_version_snapshot
    with app.app_context():
        p = db.session.get(Product, sample_product)
        # Clear any existing versions
        ProductVersion.query.filter_by(product_id=sample_product).delete()
        db.session.commit()

        save_version_snapshot(p, label='Test major', is_major=True)
        v = ProductVersion.query.filter_by(product_id=sample_product).first()
        assert v is not None
        assert v.is_major is True
        assert v.version_num == 1
        assert v.pis_data is not None


def test_minor_snapshot_increments_version(app, sample_product):
    from model import db, Product, ProductVersion
    from helpers import save_version_snapshot
    with app.app_context():
        p = db.session.get(Product, sample_product)
        ProductVersion.query.filter_by(product_id=sample_product).delete()
        db.session.commit()

        save_version_snapshot(p, label='v1', is_major=True)
        save_version_snapshot(p, label='v2 draft', is_major=False)

        versions = ProductVersion.query.filter_by(
            product_id=sample_product).order_by(ProductVersion.version_num).all()
        assert len(versions) == 2
        assert versions[0].version_num == 1
        assert versions[1].version_num == 2
        assert versions[1].is_major is False


# ── _compute_shallow_diff ─────────────────────────────────────────────────────

def test_shallow_diff_detects_changed_key():
    from helpers import _compute_shallow_diff
    old = {'a': 1, 'b': 'hello'}
    new = {'a': 1, 'b': 'world'}
    diff = _compute_shallow_diff(old, new)
    assert diff == {'b': 'world'}


def test_shallow_diff_empty_when_identical():
    from helpers import _compute_shallow_diff
    data = {'x': [1, 2], 'y': {'z': 3}}
    assert _compute_shallow_diff(data, data) == {}


def test_shallow_diff_detects_new_key():
    from helpers import _compute_shallow_diff
    diff = _compute_shallow_diff({'a': 1}, {'a': 1, 'b': 2})
    assert diff == {'b': 2}


def test_shallow_diff_detects_removed_key():
    from helpers import _compute_shallow_diff
    diff = _compute_shallow_diff({'a': 1, 'b': 2}, {'a': 1})
    assert diff == {'b': None}


# ── normalize_pis_data ────────────────────────────────────────────────────────

def test_normalize_fills_all_required_keys():
    from helpers import normalize_pis_data
    result = normalize_pis_data({})
    assert 'header_info' in result
    assert 'range_overview' in result
    assert 'sales_arguments' in result
    assert isinstance(result['sales_arguments'], list)
    assert 'technical_specifications' in result
    assert 'warranty_service' in result
    assert 'seo_data' in result


def test_normalize_preserves_existing_values():
    from helpers import normalize_pis_data
    data = {'range_overview': 'My description', 'sales_arguments': ['Fast', 'Reliable']}
    result = normalize_pis_data(data)
    assert result['range_overview'] == 'My description'
    assert result['sales_arguments'] == ['Fast', 'Reliable']


def test_normalize_handles_none_input():
    from helpers import normalize_pis_data
    result = normalize_pis_data(None)
    assert isinstance(result, dict)
    assert 'header_info' in result


# ── validate_pis_data ─────────────────────────────────────────────────────────

def test_validate_pis_data_passes_for_valid_structure():
    from utils.validation import validate_pis_data
    valid = {
        'header_info': {'product_name': 'Widget', 'brand': 'Acme',
                        'model_number': 'W1', 'price_estimate': ''},
        'range_overview': 'A great widget.',
        'sales_arguments': ['Durable'],
        'technical_specifications': {'Weight': '1kg'},
        'warranty_service': {'period': '1 year', 'coverage': 'Full'},
    }
    ok, warnings = validate_pis_data(valid)
    assert ok is True
    assert warnings == []


def test_validate_pis_data_warns_on_missing_keys():
    from utils.validation import validate_pis_data
    ok, warnings = validate_pis_data({'header_info': {'product_name': 'X', 'brand': 'Y'}})
    assert ok is False
    assert any('sales_arguments' in w for w in warnings)


def test_validate_pis_data_rejects_non_dict():
    from utils.validation import validate_pis_data
    ok, warnings = validate_pis_data("not a dict")
    assert ok is False
    assert warnings


def test_validate_pis_data_warns_on_wrong_type():
    from utils.validation import validate_pis_data
    _, warnings = validate_pis_data({
        'header_info': {'product_name': 'X', 'brand': 'Y'},
        'range_overview': '',
        'sales_arguments': 'should be a list',  # wrong type
        'technical_specifications': {},
        'warranty_service': {},
    })
    assert any('sales_arguments' in w for w in warnings)


# ── validate_spec_data ────────────────────────────────────────────────────────

def test_validate_spec_data_passes_for_valid_structure():
    from utils.validation import validate_spec_data
    valid = {
        'header_info': {'product_name': 'Widget', 'brand': 'Acme'},
        'customer_friendly_description': 'Great widget for the home.',
        'key_features': ['Feature A', 'Feature B'],
        'technical_specifications': {'Weight': '1kg'},
    }
    ok, warnings = validate_spec_data(valid)
    assert ok is True
    assert warnings == []


def test_validate_spec_data_warns_on_missing_keys():
    from utils.validation import validate_spec_data
    ok, warnings = validate_spec_data({})
    assert ok is False
    assert len(warnings) >= 4


def test_validate_spec_data_rejects_non_dict():
    from utils.validation import validate_spec_data
    ok, warnings = validate_spec_data(None)
    assert ok is False
