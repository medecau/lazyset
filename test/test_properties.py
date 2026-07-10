"""Property and regression tests for the pure helpers in ``dataset.util``.

These functions have no database dependency, so they are tested in isolation
here rather than through the DB fixtures. The regression tests below pin the
three bugs fixed alongside them; the Hypothesis property tests assert the
ideal invariants across generated inputs.
"""

from urllib.parse import urlparse

import pytest

from dataset.util import (
    index_name,
    normalize_column_name,
    safe_url,
)

# ---------------------------------------------------------------------------
# Regression tests for the three util.py fixes.
# ---------------------------------------------------------------------------


def test_safe_url_masks_userinfo_password():
    """The password in the userinfo is always replaced by the mask."""
    out = safe_url("postgresql://user:secret@host:5432/db")
    assert urlparse(out).password == "*****"
    assert "secret" not in out


def test_safe_url_preserves_path_and_query():
    """A ``:pw@`` sequence in the path/query must not be scrubbed.

    The previous implementation used a global ``str.replace(':pw@', ...)``
    which mangled any matching sequence outside the userinfo.
    """
    url = "postgresql://user:secret@host/db?redirect=svc:secret@example.com"
    out = safe_url(url)
    assert urlparse(out).password == "*****"
    # The query value is left untouched.
    assert "svc:secret@example.com" in out


def test_safe_url_no_password_is_noop():
    for url in ("sqlite:///:memory:", "postgresql://user@host/db"):
        assert safe_url(url) == url


def test_normalize_column_name_rejects_dot_past_truncation():
    """A trailing '.' beyond byte 63 must still be rejected.

    Previously the ``[:63]`` slice ran before the charset check, so a long
    name ending in '.' was silently truncated-and-accepted.
    """
    with pytest.raises(ValueError):
        normalize_column_name("a" * 63 + ".")
    with pytest.raises(ValueError):
        normalize_column_name("a" * 70 + "-")


def test_index_name_distinct_for_ambiguous_columns():
    """Column lists that differ only in '||' placement must not collide."""
    assert index_name("t", ["a", "b||c"]) != index_name("t", ["a||b", "c"])
