"""One-off smoke test for utils/compare deterministic fallback + exports.
Run via venv python; verifies CSV/XLSX builders without touching the DB or
calling Gemini."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
from utils.compare import (
    _deterministic_align, _merge_fallback_into_ai, _order_rows,
    build_csv, build_xlsx,
)


class FakeProduct:
    def __init__(self, pid, name, specs, brand='', model_number=''):
        self.id = pid
        self.model_name = name
        self.image_path = None
        self.pis_data = {
            'header_info': {'brand': brand, 'model_number': model_number},
            'technical_specifications': specs,
        }
        self.deleted_at = None


with app.app_context():
    products = [
        FakeProduct(12, 'Samsung RB Fridge',
                    {'Power Consumption': '120 W', 'Capacity': '350 L', 'Defrost Type': 'Auto'},
                    brand='Samsung', model_number='RB-350'),
        FakeProduct(18, 'LG GBB Fridge',
                    {'Wattage': '115W', 'Net Volume': '340L', 'Energy Class': 'A++'},
                    brand='LG', model_number='GBB-340'),
    ]
    aligned = _deterministic_align(products)
    aligned = _merge_fallback_into_ai(aligned, products)
    aligned['rows'] = _order_rows(aligned['rows'])

    table = {
        'products': [
            {
                'id': str(p.id),
                'name': p.model_name,
                'brand': p.pis_data['header_info']['brand'],
                'model_number': p.pis_data['header_info']['model_number'],
                'image_url': '',
            } for p in products
        ],
        'rows': aligned['rows'],
        'sections': [],
    }

    csv_out = build_csv(table)
    print('--- CSV (first 800 chars) ---')
    print(csv_out[:800])

    xlsx_bytes = build_xlsx(table)
    print(f'--- XLSX size: {len(xlsx_bytes)} bytes ---')
    assert xlsx_bytes[:2] == b'PK', 'Not a valid xlsx (missing zip header)'
    print('OK')
