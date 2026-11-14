#!/usr/bin/env python
"""Обёртка валидатора политики. Логика — в пакете partner_cli.

python tools/policy_lint.py policy.yaml [--as-of YYYY-MM-DD]
(в клиентских репозиториях: python -m partner_cli.policy_lint policy.yaml)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

try:
    from partner_cli.policy_lint import main
except ImportError:
    print("ERROR: установите пакет (pip install -e .[dev]) или: pip install pyyaml jsonschema")
    sys.exit(1)

main()
