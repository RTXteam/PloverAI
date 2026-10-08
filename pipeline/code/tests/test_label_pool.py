# stage 7 gate over the entity's whole name pool. cases are the real
# round-3 pilot failures plus the fragment trap the pool must not reopen.
from pipeline.code.pipeline import LOW_CONFIDENCE_THRESHOLD, _best_label_match


def test_sle_stray_canonical_label_rescued_by_synonym() -> None:
    sim, best, dbg = _best_label_match(
        "systemic lupus erythematosus", "EXCESS LMW-DNA",
        ["SLE", "lupus", "Systemic Lupus Erythematosus"],
    )
    assert sim >= LOW_CONFIDENCE_THRESHOLD
    assert best == "Systemic Lupus Erythematosus"
    assert dbg["n_labels_checked"] == 4


def test_ocd_rescued_by_equivalent_label() -> None:
    sim, best, _ = _best_label_match(
        "obsessive-compulsive disorder", "Compulsion",
        ["OCD", "obsessive-compulsive disorder"],
    )
    assert sim == 1.0
    assert best == "obsessive-compulsive disorder"


def test_fragment_synonyms_do_not_rescue_a_wrong_entity() -> None:
    # round-2 r02 trap in reverse: "syndrome" is a fragment of the mention
    sim, best, _ = _best_label_match(
        "marfan syndrome", "urinary system disorder", ["syndrome", "urinary"],
    )
    assert sim < LOW_CONFIDENCE_THRESHOLD
    assert best == "urinary system disorder"


def test_short_synonym_fragment_rejected() -> None:
    sim, _, _ = _best_label_match(
        "systemic lupus erythematosus", "excess lmw-dna", ["lupus", "SLE"],
    )
    assert sim < LOW_CONFIDENCE_THRESHOLD


def test_primary_label_keeps_the_typo_tolerant_rule() -> None:
    sim, best, _ = _best_label_match("alzhiemers", "Alzheimer disease", [])
    assert best == "Alzheimer disease"
    assert sim == _best_label_match("alzhiemers", "Alzheimer disease", ["x"])[0]


def test_primary_perfect_match_is_not_overridden() -> None:
    sim, best, _ = _best_label_match("seizures", "Seizure", ["Seizures", "convulsion"])
    assert sim >= LOW_CONFIDENCE_THRESHOLD
    assert best == "Seizure"
