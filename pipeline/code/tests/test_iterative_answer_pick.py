# iterative Stage-11 loop control: the pure helper that validates which
# picks survive (the relevance-stop is now just the LLM's confidence flag,
# checked inline). the LLM call and loop wiring are integration-level; here
# we pin the deterministic carry-forward validation. no network.

from code.pipeline import _valid_iterative_picks


def test_valid_picks_keeps_chunk_and_carry_forward_drops_invented():
    answers = [
        {"curie": "CHEBI:1", "label": "in chunk"},
        {"curie": "CHEBI:2", "label": "carried from earlier chunk"},
        {"curie": "CHEBI:999", "label": "invented, not anywhere"},
        {"label": "no curie"},
        "not a dict",
    ]
    out = _valid_iterative_picks(
        answers, chunk_node_ids={"CHEBI:1"}, prior_curies={"CHEBI:2"},
    )
    assert [a["curie"] for a in out] == ["CHEBI:1", "CHEBI:2"]


def test_valid_picks_dedupes_by_curie_first_wins():
    answers = [
        {"curie": "CHEBI:1", "label": "first"},
        {"curie": "CHEBI:1", "label": "dup"},
    ]
    out = _valid_iterative_picks(answers, chunk_node_ids={"CHEBI:1"}, prior_curies=set())
    assert len(out) == 1
    assert out[0]["label"] == "first"
