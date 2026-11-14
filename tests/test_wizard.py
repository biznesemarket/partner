"""T-07: демо-профиль мастера не содержит персональных данных.

Сценарии:
- W-01  demo() обезличен: нет ФИО директора и названия пилотного клиента
- W-02  обезличенный demo проходит валидацию и собирает политику
"""
from __future__ import annotations

from partner_cli import wizard


def test_demo_profile_has_no_personal_data():
    answers = wizard.WizardAnswers.demo()
    assert "Потапов" not in answers.approved_by
    assert "ФОНИИ" not in answers.tenant_name
    assert answers.approved_by == "Директор (демо)"
    assert answers.tenant_name == "ООО «Демо»"


def test_demo_profile_is_valid_and_builds_policy():
    answers = wizard.WizardAnswers.demo()
    assert wizard.validate(answers) == []
    policy = wizard.build_policy(answers)
    assert policy["meta"]["approved_by"] == "Директор (демо)"
    assert policy["meta"]["tenant_id"] == "ООО «Демо»"
    assert policy["legal"]["enabled"] is False
