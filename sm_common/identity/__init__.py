"""Cross-service identity contract helpers.

This module is the canonical home for the SpatiaMed identity contract. See the
"Identity contract" section of the README for the narrative version:

  * ``tenant_id`` (minted by platform-api) IS ``hospital_id`` (persisted by
    QueueCare) — the same UUID renamed at the ``/tenant-created`` seam.
  * Different hash *families* (phone vs staff email) MUST use different salts so a
    phone hash and an email hash can never be interchanged or cross-referenced.
    :func:`assert_distinct_salts` is the startup guard for that invariant.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["assert_distinct_salts"]


def assert_distinct_salts(*salts: str, names: Sequence[str] | None = None) -> None:
    """Assert that a set of salts that are REQUIRED to differ are not accidentally equal.

    Intended as a startup / config-validation check. For example, platform-api's
    staff ``email_hash`` salt and the phone-hash salt must be intentionally separate
    so email and phone hashes live in disjoint spaces and can never be confused for
    one another. If a misconfiguration set both from the same env var, this raises
    loudly at boot instead of silently producing interchangeable hashes.

    Args:
        *salts: Two or more salt values that must all be distinct.
        names: Optional human-readable labels, one per salt (same order), used to
            make the error message point at the offending pair. If provided, its
            length must equal the number of salts.

    Raises:
        ValueError: If fewer than two salts are given, if ``names`` length does not
            match, or if any two salts are identical.
    """
    if len(salts) < 2:
        raise ValueError("assert_distinct_salts requires at least two salts")
    if names is not None and len(names) != len(salts):
        raise ValueError(
            f"names has {len(names)} labels but {len(salts)} salts were given"
        )

    seen: dict[str, int] = {}
    for i, salt in enumerate(salts):
        if salt in seen:
            j = seen[salt]
            label_i = names[i] if names is not None else f"salt[{i}]"
            label_j = names[j] if names is not None else f"salt[{j}]"
            raise ValueError(
                f"Salts {label_j!r} and {label_i!r} must be distinct but are "
                f"identical — email and phone (or other) hash families must not "
                f"share a salt."
            )
        seen[salt] = i
