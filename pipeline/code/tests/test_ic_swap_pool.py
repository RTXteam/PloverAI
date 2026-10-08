# stage 5 swap-eligibility: the IC re-rank may only move the pick to a
# naming variant of the user's mention, never to a generic fragment or
# an unrelated low-IC concept. cases are the real pilot failures.
from pipeline.code.pipeline import _is_same_concept_variant


def test_plural_singular_variant_allowed() -> None:
    assert _is_same_concept_variant("seizures", "Seizure")


def test_longer_canonical_form_allowed() -> None:
    assert _is_same_concept_variant("diabetes", "diabetes mellitus")


def test_generic_fragment_rejected() -> None:
    # round-2 r02: "syndrome" is inside "marfan syndrome" but is not it
    assert not _is_same_concept_variant("marfan syndrome", "syndrome")


def test_unrelated_generic_rejected() -> None:
    # round-2 r04: Avitaminosis is broader than, not a variant of, the mention
    assert not _is_same_concept_variant("vitamin d deficiency", "Avitaminosis")


def test_one_letter_sibling_rejected() -> None:
    # round-2 r04 re-run: 95% character-similar, different vitamin
    assert not _is_same_concept_variant("vitamin d deficiency", "vitamin B deficiency")


def test_narrower_form_still_eligible_but_ic_sort_decides() -> None:
    # eligibility only; the IC sort picks the broadest among eligible
    assert _is_same_concept_variant("diabetes", "type 2 diabetes mellitus")


def test_unrelated_short_label_rejected() -> None:
    # round-2 r12: albumin for an HPV vaccine
    assert not _is_same_concept_variant("human papillomavirus vaccine", "alb")


def test_empty_inputs_rejected() -> None:
    assert not _is_same_concept_variant("", "Seizure")
    assert not _is_same_concept_variant("seizures", "")
