from pipeline.code.pipeline import _normalise_answers


def test_dict_items_kept_verbatim() -> None:
    items = [{"curie": "CHEBI:6801", "label": "metformin", "supporting_edge_ids": ["1"]}]
    assert _normalise_answers(items) == items


def test_bare_curie_strings_become_objects() -> None:
    # 13-slug smoke, deepseek-v4-flash-0731 returned a list of strings
    out = _normalise_answers(["CHEBI:6801", " CHEBI:5441 "])
    assert [a["curie"] for a in out] == ["CHEBI:6801", "CHEBI:5441"]
    assert out[0]["supporting_edge_ids"] == []


def test_dict_keyed_by_curie_accepted() -> None:
    out = _normalise_answers({"CHEBI:6801": {"label": "metformin"}, "CHEBI:1": "x"})
    assert [a["curie"] for a in out] == ["CHEBI:6801", "CHEBI:1"]
    assert out[0]["label"] == "metformin"


def test_garbage_items_dropped_and_non_list_is_empty() -> None:
    assert _normalise_answers([None, 3, {"label": "no curie"}, ""]) == []
    assert _normalise_answers("CHEBI:6801") == []
    assert _normalise_answers(None) == []
