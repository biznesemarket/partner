"""Версии пакетов и OpenAPI не расходятся с релизом (v1.3.0)."""
from __future__ import annotations

from importlib.metadata import version as distribution_version

import partner_agent_kit
import partner_cli
import partner_connectors
import partner_server
from partner_server.main import app


def test_package_versions_match_distribution():
    expected = distribution_version("partner-tracker")
    assert {
        partner_cli.__version__,
        partner_server.__version__,
        partner_agent_kit.__version__,
        partner_connectors.__version__,
    } == {expected}
    assert app.version == expected
