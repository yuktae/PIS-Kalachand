"""
JSONB field validation for Product.pis_data and Product.spec_data.

Validates at the save boundary (save_version_snapshot) to catch malformed AI
responses before they reach the database. Uses plain Python — no new dependency.

All validators return (is_valid: bool, warnings: list[str]).
Warnings are logged but never raise — the save proceeds regardless, since
blocking a save on bad AI output is worse than storing imperfect data.
"""
from __future__ import annotations


def validate_pis_data(data: object) -> tuple[bool, list[str]]:
    """Validate the structure of pis_data."""
    warnings: list[str] = []

    if not isinstance(data, dict):
        return False, ['pis_data must be a dict']

    # Required top-level keys
    for key in ('header_info', 'range_overview', 'sales_arguments',
                'technical_specifications', 'warranty_service'):
        if key not in data:
            warnings.append(f'pis_data missing expected key: {key}')

    # Type checks
    if 'header_info' in data:
        if not isinstance(data['header_info'], dict):
            warnings.append('pis_data.header_info must be a dict')
        else:
            for sub in ('product_name', 'brand'):
                if not data['header_info'].get(sub):
                    warnings.append(f'pis_data.header_info.{sub} is empty')

    if 'sales_arguments' in data and not isinstance(data['sales_arguments'], list):
        warnings.append('pis_data.sales_arguments must be a list')

    if 'technical_specifications' in data and not isinstance(data['technical_specifications'], dict):
        warnings.append('pis_data.technical_specifications must be a dict')

    if 'warranty_service' in data and not isinstance(data['warranty_service'], dict):
        warnings.append('pis_data.warranty_service must be a dict')

    return len(warnings) == 0, warnings


def validate_spec_data(data: object) -> tuple[bool, list[str]]:
    """Validate the structure of spec_data."""
    warnings: list[str] = []

    if not isinstance(data, dict):
        return False, ['spec_data must be a dict']

    for key in ('header_info', 'customer_friendly_description',
                'key_features', 'technical_specifications'):
        if key not in data:
            warnings.append(f'spec_data missing expected key: {key}')

    if 'key_features' in data and not isinstance(data['key_features'], list):
        warnings.append('spec_data.key_features must be a list')

    if 'technical_specifications' in data and not isinstance(data['technical_specifications'], dict):
        warnings.append('spec_data.technical_specifications must be a dict')

    if 'seo' in data and not isinstance(data['seo'], dict):
        warnings.append('spec_data.seo must be a dict')

    if 'categories' in data and not isinstance(data['categories'], dict):
        warnings.append('spec_data.categories must be a dict')

    return len(warnings) == 0, warnings
