"""D-05: OpenAPI-выжимка синхронна с фактическим app.openapi()."""
from __future__ import annotations

import sys
from pathlib import Path

from partner_server.main import app

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import openapi_digest as digest  # noqa: E402


def test_api_reference_matches_app_openapi():
    expected = digest.render_digest(app.openapi())
    actual = digest.DOC_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        "docs/03-architecture/api-reference.md устарел: "
        "запустите python tools/openapi_digest.py"
    )


def test_api_reference_lists_every_operation():
    spec = app.openapi()
    document = digest.DOC_PATH.read_text(encoding="utf-8")
    for path, operations in spec["paths"].items():
        for method in operations:
            if method.lower() in digest.METHOD_ORDER:
                assert f"| {method.upper()} | `{path}` |" in document


def test_api_reference_has_error_codes_and_auth():
    document = digest.DOC_PATH.read_text(encoding="utf-8")
    assert "PARTNER_TENANT_TOKENS" in document
    assert "manual_only_policy_not_supported" in document
    assert "participant_scope_denied" in document
