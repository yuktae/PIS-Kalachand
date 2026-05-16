"""Freeze Brave web_context for every fixture under fixtures/.

Run once after adding new fixtures (or when you want to refresh the
captured web text for an existing one):

    python tests/ai_tests/freeze_fixtures.py
    python tests/ai_tests/freeze_fixtures.py --only single_poco_x7
    python tests/ai_tests/freeze_fixtures.py --refresh    # re-fetch even if cached

This is a one-time setup cost — after freezing, every pytest run uses the
saved web_context.txt and never hits Brave (deterministic + free).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from conftest import _discover_fixtures, _freeze_web_context, FIXTURES_DIR


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--only", help="Freeze only this fixture name")
    p.add_argument("--refresh", action="store_true",
                    help="Re-fetch even if web_context.txt already has content")
    args = p.parse_args()

    fixtures = _discover_fixtures(FIXTURES_DIR)
    if args.only:
        fixtures = [f for f in fixtures if f.name == args.only]
        if not fixtures:
            print(f"No fixture named {args.only!r} found.")
            return 1

    print(f"Freezing web_context for {len(fixtures)} fixture(s)...\n")
    for f in fixtures:
        already = f.web_context.strip()
        if already and not args.refresh:
            print(f"  SKIP  {f.name:30s} (already frozen, {len(already)} chars)")
            continue
        print(f"  FETCH {f.name:30s} -> Brave for {f.product_name!r}")
        text = _freeze_web_context(f)
        if text:
            print(f"        captured {len(text)} chars")
        else:
            print(f"        WARNING: empty result (Brave returned nothing)")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
