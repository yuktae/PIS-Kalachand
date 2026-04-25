"""
Utils package for PIS System
Contains helper functions organized by functionality
"""

from .image_processing import (
    extract_domain,
    search_google_api,
    search_duckduckgo,
    clean_search_query,
    ai_validate_image,
    download_image_bytes,
    find_best_images,
    find_and_validate_image,
    find_image_simple,
    download_web_image
)

from .web_scraping import scrape_url_data, scrape_url_data_deep

from .ai_generation import (
    generate_pis_data,
    generate_comprehensive_spec_data,
    generate_bulk_pis_data,
    generate_specsheet_optimization,
    generate_ai_revision
)

from .pdf_processing import extract_specific_image

from .history import log_event

__all__ = [
    # Image processing
    'extract_domain',
    'search_google_api',
    'clean_search_query',
    'ai_validate_image',
    'download_image_bytes',
    'find_best_images',
    'find_and_validate_image',
    'find_image_simple',
    'search_duckduckgo',
    'download_web_image',
    # Web scraping
    'scrape_url_data',
    'scrape_url_data_deep',
    # AI generation
    'generate_pis_data',
    'generate_comprehensive_spec_data',
    'generate_bulk_pis_data',
    'generate_specsheet_optimization',
    'generate_ai_revision',
    # PDF processing
    'extract_specific_image',
    # History
    'log_event'
]
