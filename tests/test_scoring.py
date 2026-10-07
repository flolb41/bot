from app.scoring import compute_score, score_from_project_row


def test_compute_score_max_clamped_to_100():
    breakdown = compute_score(
        reward_potential_high=True,
        truly_free=True,
        official_active=True,
        short_window=True,
        early_testnet=True,
        low_competition=True,
        automatable=True,
    )
    # 25+20+15+15+10+10+5 = 100 pile, donc pas de clamp nécessaire ici mais vérifie la borne.
    assert breakdown.total == 100
    assert breakdown.classify().value == "PRIORITÉ CRITIQUE"


def test_compute_score_never_negative():
    breakdown = compute_score(
        truly_free=False,
        official_active=False,
        requires_deposit=True,
        requires_kyc=True,
        high_fees=True,
        unverified_contract=True,
        asks_for_seed=True,
        is_suspicious=True,
    )
    assert breakdown.total == 0
    assert breakdown.classify().value == "IGNORER"


def test_malus_demande_seed_is_heaviest():
    with_seed = compute_score(reward_potential_high=True, asks_for_seed=True)
    without_seed = compute_score(reward_potential_high=True)
    assert with_seed.total < without_seed.total


def test_score_from_project_row_requires_capital_malus():
    free_project = {"requires_capital": 0, "requires_kyc": 0, "estimated_cost": 0, "status": "testnet"}
    paid_project = {"requires_capital": 1, "requires_kyc": 0, "estimated_cost": 0, "status": "testnet"}

    free_score = score_from_project_row(free_project)
    paid_score = score_from_project_row(paid_project)

    assert free_score.total > paid_score.total
