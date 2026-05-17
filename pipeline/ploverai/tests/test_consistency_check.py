# Stage 7 label-vs-mention consistency check.
#
# this is the safety net that prevents the diabetes-typo class of bug:
# user typed "type 2 diabites", NameRes returned "sialidosis type 2",
# the pipeline silently queried for the wrong disease, and the LLM
# reported "no treatments found for type 2 diabetes" — confidently
# wrong. the check should return a similarity score that's BELOW the
# threshold for that input pair, so the pipeline can refuse to query
# PloverDB with a probably-wrong entity.
#
# the function is pure (stdlib SequenceMatcher only), so this whole
# file tests behaviour with zero LLM and zero network. assertions are
# tight: exact similarity values, not "less than threshold". if the
# code's similarity for a known input changes from 0.38 to 0.42, this
# test fires and the user investigates whether the change is intended.

import pytest

from ploverai.pipeline import (
    _check_label_consistency,
    LOW_CONFIDENCE_THRESHOLD,
)


# ---- the regression case the whole layer exists for ----

# user's actual failure: "type 2 diabites" (typo) resolved to "sialidosis
# type 2" via NameRes BM25. the similarity must be BELOW the threshold so
# Stage 7 fires and the pipeline refuses to query KG with the wrong
# disease.
def test_diabetes_typo_failure_is_caught():
    sim, dbg = _check_label_consistency(
        mention="type 2 diabites",
        label="sialidosis type 2",
    )
    # exact value, not a range — if this drifts the test fires.
    # 0.38 = SequenceMatcher.ratio("type 2 diabites", "sialidosis type 2")
    assert sim == pytest.approx(0.38, abs=0.01)
    # and it must actually be below the threshold (otherwise the whole
    # safety net is useless)
    assert sim < LOW_CONFIDENCE_THRESHOLD
    # substring is false because neither string contains the other
    assert dbg["substring_match"] is False


# ---- positive cases: things that look noisy but ARE the same entity ----

# typo that's a single-letter edit ("warfrin" → "warfarin"). seqmatcher
# should give a high score, similar to "warfa(r)in" with one letter
# missing.
def test_warfarin_one_letter_typo_passes():
    sim, dbg = _check_label_consistency(
        mention="warfrin",
        label="warfarin",
    )
    assert sim == pytest.approx(0.93, abs=0.02)
    assert sim >= LOW_CONFIDENCE_THRESHOLD


# transposition typo ("imatanib" / "imatinab" instead of "imatinib")
def test_imatinib_transposition_passes():
    sim_a, _ = _check_label_consistency("imatanib", "Imatinib")
    sim_b, _ = _check_label_consistency("imatinab", "Imatinib")
    assert sim_a >= LOW_CONFIDENCE_THRESHOLD
    assert sim_b >= LOW_CONFIDENCE_THRESHOLD


# substring containment — user types a short form, ontology has the long
# canonical form. should pass even though seqmatcher is mediocre.
def test_substring_makes_similarity_one():
    # user mention is fully contained in the label
    sim, dbg = _check_label_consistency(
        mention="type 2 diabetes",
        label="type 2 diabetes mellitus",
    )
    # substring path forces similarity to 1.0 regardless of seqmatcher
    assert sim == 1.0
    assert dbg["substring_match"] is True
    # seqmatcher itself would be < 1 here; check it's the substring
    # path that's carrying us, not seqmatcher
    assert dbg["seqmatcher_ratio"] < 1.0


# exact match (modulo case)
def test_exact_match_case_insensitive():
    sim, dbg = _check_label_consistency("Cystic Fibrosis", "cystic fibrosis")
    assert sim == 1.0


# ---- behavioral invariants that constrain future refactors ----

# Jaccard / token-overlap was DELIBERATELY removed because it gave a
# false 0.5 to the diabetes-typo case. this test pins that decision: if
# someone adds token-overlap back into the max, the typo case will jump
# from 0.38 to 0.50 and THIS assertion will catch it. it's a guardrail
# against a known anti-pattern.
def test_token_overlap_is_not_a_signal():
    sim, dbg = _check_label_consistency(
        mention="type 2 diabites",
        label="sialidosis type 2",
    )
    # if token-overlap (Jaccard on {type,2,diabites} ∩ {sialidosis,type,2})
    # were a signal, similarity would be max(seq, jaccard, ...) = 0.50.
    # require strictly less than that — only the seqmatcher path is alive.
    assert sim < 0.50


# both inputs empty → similarity 0 (NOT 1, even though empty contains
# empty in the trivial set-theory sense). substring contains is false
# when either side is empty.
def test_empty_inputs_score_zero():
    sim, dbg = _check_label_consistency("", "")
    assert sim == 0.0
    assert dbg["substring_match"] is False


# whitespace and case should be normalized away — leading/trailing
# spaces and uppercase should not change the score.
def test_normalization_strips_case_and_whitespace():
    sim_a, _ = _check_label_consistency("  WARFARIN  ", "warfarin")
    sim_b, _ = _check_label_consistency("warfarin", "warfarin")
    assert sim_a == sim_b == 1.0


# symmetry: order of args should not change the score. seqmatcher is
# symmetric by construction; substring containment is symmetric because
# we check both directions.
def test_symmetric_in_arguments():
    a, _ = _check_label_consistency("type 2 diabetes", "type 2 diabetes mellitus")
    b, _ = _check_label_consistency("type 2 diabetes mellitus", "type 2 diabetes")
    assert a == b == 1.0


# ---- edge case: label-collision (the "seizures" failure mode)
# This is NOT a Stage 7 failure; it's a Stage 4 (LLM candidate-pick)
# concern. Stage 7 should PASS this because the mention is a substring
# of the label — the strings are textually consistent even if the
# resolved entity is semantically wrong (a Disease called "seizures, ..."
# vs the phenotype "Seizure"). this test pins that boundary: 0.8 does
# NOT police semantic correctness, only textual divergence.
def test_label_collision_is_not_a_0_8_concern():
    sim, dbg = _check_label_consistency(
        mention="seizures",
        label="seizures, benign familial neonatal, 1",
    )
    # substring=True → similarity 1.0 → Stage 7 passes
    assert dbg["substring_match"] is True
    assert sim == 1.0
    # Layer 2 (candidate-pick) is responsible for catching that this is
    # the wrong entity. not Layer 3.
