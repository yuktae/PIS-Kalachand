"""On-disk cache for Anthropic judge calls.

Stops you re-spending $0.30+ on every test run while iterating. Cache lives
per-fixture so deleting one fixture's cache only invalidates its judgments.

Cache key = sha256 of (judge_name, sources_hash, payload_dict_as_json).
When the proforma text or web_context changes (i.e. pis_data.json is
re-generated), the sources_hash flips and the cache key misses — judgments
re-run automatically. No mtime tricks, no manual invalidation needed.

Schema (fixtures/<name>/judge_<layer>_cache.json):
{
  "schema_version": 1,
  "judgments": {
    "<key>": {
      "judge": "haiku_field_rescue",
      "payload": { ... },          # echo of inputs for debuggability
      "verdict": { ... },          # what the judge returned
      "judged_at": "2026-05-16T..."
    }
  }
}
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1


def sources_hash(proforma_text: str, web_context: str) -> str:
    """Stable fingerprint of the two source blocks. Used as the cache-key
    prefix so judgments invalidate when either source changes."""
    h = hashlib.sha256()
    h.update(b"proforma:")
    h.update((proforma_text or "").encode("utf-8"))
    h.update(b"\nweb:")
    h.update((web_context or "").encode("utf-8"))
    return h.hexdigest()[:16]


def _key(judge: str, srcs_hash: str, payload: dict) -> str:
    h = hashlib.sha256()
    h.update(judge.encode("utf-8"))
    h.update(b"\n")
    h.update(srcs_hash.encode("utf-8"))
    h.update(b"\n")
    h.update(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()


def load(cache_path: Path) -> dict:
    if not cache_path.exists():
        return {"schema_version": SCHEMA_VERSION, "judgments": {}}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if data.get("schema_version") == SCHEMA_VERSION:
            return data
    except Exception:
        pass
    return {"schema_version": SCHEMA_VERSION, "judgments": {}}


def save(cache_path: Path, data: dict) -> None:
    cache_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def judged_or_run(
    cache_path: Path,
    judge: str,
    srcs_hash: str,
    payload: dict,
    run_fn: Callable[[], dict],
) -> dict:
    """Return cached verdict for (judge, srcs_hash, payload) if present.
    Otherwise call run_fn(), persist the result, and return it.

    Args:
        cache_path: per-fixture cache file path
        judge: short name like "haiku_field_rescue" / "sonnet_claim"
        srcs_hash: from sources_hash(proforma_text, web_context)
        payload: dict that's both part of the cache key AND echoed back into
                 the entry for debuggability (e.g. {"field": x, "value": y})
        run_fn: thunk that returns the verdict dict
    """
    data = load(cache_path)
    key = _key(judge, srcs_hash, payload)
    entry = data["judgments"].get(key)
    if entry is not None and "verdict" in entry:
        return entry["verdict"]
    verdict = run_fn()
    data["judgments"][key] = {
        "judge": judge,
        "payload": payload,
        "verdict": verdict,
        "judged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save(cache_path, data)
    return verdict
