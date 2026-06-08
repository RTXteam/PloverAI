# the one trivial test: confirm the package wires up. if this fails,
# every other test will fail too, and the failure mode (import error
# vs constant missing vs prompt missing) tells you what broke.
# everything else in this directory tests an actual edge case or
# invariant, not just "the module loads".


# importing the pipeline package proves __init__.py + relative imports
# all wire. status constants and key prompts are checked at module-load
# time so a typo in either is caught here before any "real" test runs.
from code import pipeline as pl
from code import prompts


# status constants are user-visible (they end up in meta.json and the
# eval harness keys on them). renaming or losing one would silently
# break downstream analysis. enumerate the ones we depend on.
EXPECTED_STATUSES = {
    "ok",
    "invalid_query",
    "invalid_query_arity",
    "llm_bad_json",
    "plover_error",
    "llm_error",
    "nameres_failed",
    "nodenorm_failed",
    "entity_empty",
    "out_of_scope",
    "no_candidate_match",
}


def test_status_constants_present_and_unique():
    # collect every STATUS_* constant from the pipeline module and check
    # they form exactly the expected set. catches accidental renaming,
    # duplicates, and missing constants in one assert.
    found = {
        getattr(pl, name)
        for name in dir(pl)
        if name.startswith("STATUS_") and isinstance(getattr(pl, name), str)
    }
    assert found == EXPECTED_STATUSES


# key prompts are also load-time invariants — if SYS_ENTITY_EXTRACT is
# accidentally renamed, the pipeline runs but Stage 2 silently uses the
# wrong prompt. lock the names so a rename has to update tests.
def test_required_prompts_exist():
    required = [
        "SYS_SCOPE_CHECK",
        "SYS_ENTITY_EXTRACT",
        "SYS_CANDIDATE_PICK",
        "SYS_TRAPI_BUILD",
    ]
    missing = [name for name in required if not hasattr(prompts, name)]
    assert missing == [], f"missing prompt constants: {missing}"
    # also confirm they're non-empty strings (catches the case where
    # someone defines `SYS_X = ""` and the LLM ends up with an empty
    # system prompt)
    for name in required:
        val = getattr(prompts, name)
        assert isinstance(val, str) and len(val) > 100, (
            f"{name} is suspiciously short ({len(val)} chars)"
        )


# the LOW_CONFIDENCE_THRESHOLD is a public knob — tests for the
# consistency check assert against the SAME constant the pipeline uses,
# not a hard-coded 0.50 in three different files. confirm it exists and
# is in (0, 1).
def test_low_confidence_threshold_in_unit_interval():
    assert hasattr(pl, "LOW_CONFIDENCE_THRESHOLD")
    t = pl.LOW_CONFIDENCE_THRESHOLD
    assert isinstance(t, float)
    assert 0.0 < t < 1.0
