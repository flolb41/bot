from app.trackers.eligibility import check_zero_cost_eligibility, is_red_flag


def test_eligible_project_has_no_reasons():
    project = {"requires_capital": 0, "requires_kyc": 0, "estimated_cost": 0}
    check = check_zero_cost_eligibility(project)
    assert check.eligible_zero_cost is True
    assert check.reasons == []


def test_requires_capital_is_rejected():
    project = {"requires_capital": 1, "requires_kyc": 0, "estimated_cost": 0}
    check = check_zero_cost_eligibility(project)
    assert check.eligible_zero_cost is False
    assert any("dépôt" in r for r in check.reasons)


def test_cost_above_threshold_is_rejected():
    project = {"requires_capital": 0, "requires_kyc": 0, "estimated_cost": 15}
    check = check_zero_cost_eligibility(project, max_acceptable_cost=0.0)
    assert check.eligible_zero_cost is False


def test_red_flag_detection():
    assert is_red_flag("Merci de fournir votre seed phrase pour continuer") is True
    assert is_red_flag("Connecte ton wallet et complète la quête") is False
