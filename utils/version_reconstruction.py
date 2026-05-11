"""Version reconstruction — Phase 3.

The save_version_snapshot system writes one of two row shapes:

  • Major snapshots — full pis_data / spec_data. Created at every stage
    transition (submit, approve, etc.). These are the "anchors" the
    cleanup process refuses to delete.

  • Minor snapshots — diff dicts containing only top-level keys that
    changed since the previous version. Created on every Save Draft so
    the database doesn't grow unbounded.

Restoring a minor snapshot in isolation would give the user a fragment.
This module rebuilds the full state at any version by:

  1. Walking BACKWARD from the target version_num until it finds a
     major snapshot (the anchor).
  2. Walking FORWARD from that anchor, merging each minor diff on top
     until it lands on the target.

If the target itself is a major snapshot, step 2 is a no-op.

Public API:
  reconstruct_version_data(product_id, version_num) -> dict | None
"""

import copy

from model import ProductVersion


def _apply_diff(base: dict | None, diff: dict | None) -> dict | None:
    """Top-level merge: diff keys overwrite base keys. Mirrors how
    _compute_shallow_diff produces the minor-snapshot shape (only the
    keys that changed are stored, full values for each key).

    Returns a new dict; doesn't mutate `base`.
    """
    if not isinstance(diff, dict) or not diff:
        return copy.deepcopy(base) if base is not None else None
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for k, v in diff.items():
        out[k] = copy.deepcopy(v)
    return out


def reconstruct_version_data(product_id: int, version_num: int) -> dict | None:
    """Return the full pis_data / spec_data / revision_data / workflow_stage
    at `version_num` for `product_id`.

    Strategy:
      • Find every version row up to and including version_num.
      • Walk forward, starting from the most recent major snapshot
        before/at version_num; apply each subsequent minor's diff.
      • Return the resulting dict. None on error or empty history.
    """
    versions = ProductVersion.query.filter(
        ProductVersion.product_id == product_id,
        ProductVersion.version_num <= version_num,
    ).order_by(ProductVersion.version_num.asc()).all()
    if not versions:
        return None

    # Find the latest major at-or-before the target. Defaults to the
    # earliest row if none is flagged major (shouldn't happen because
    # save_version_snapshot forces major for the very first save, but
    # the fallback keeps reconstruction safe under legacy data).
    anchor_idx = 0
    for i in range(len(versions) - 1, -1, -1):
        if versions[i].is_major:
            anchor_idx = i
            break

    anchor = versions[anchor_idx]
    state = {
        'pis_data':      copy.deepcopy(anchor.pis_data),
        'spec_data':     copy.deepcopy(anchor.spec_data),
        'revision_data': copy.deepcopy(anchor.revision_data),
        'workflow_stage': anchor.workflow_stage,
    }

    # Apply forward diffs from anchor+1 up to and including target.
    for v in versions[anchor_idx + 1:]:
        if v.is_major:
            # Defensive: a major version mid-chain replaces the state
            # entirely (treat its stored data as the new baseline).
            state['pis_data']      = copy.deepcopy(v.pis_data)
            state['spec_data']     = copy.deepcopy(v.spec_data)
            state['revision_data'] = copy.deepcopy(v.revision_data)
        else:
            state['pis_data']  = _apply_diff(state['pis_data'],  v.pis_data)
            state['spec_data'] = _apply_diff(state['spec_data'], v.spec_data)
            # revision_data on minor snapshots stores a full copy (see
            # helpers.save_version_snapshot), so just overwrite.
            state['revision_data'] = copy.deepcopy(v.revision_data)
        # Stage may not be stored on every minor; only update if non-null.
        if v.workflow_stage:
            state['workflow_stage'] = v.workflow_stage

    return state
