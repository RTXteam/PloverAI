# translator_assets.py: the converter that turns NCATS Translator
# "Tests" repository assets (github.com/NCATSTranslator/Tests,
# test_assets/Asset_*.json) into PloverAI gold question records.
#
# each asset is ONE labelled (input, predicate, output) triple, e.g.
# "NeverShow: Metric treats Multiple Sclerosis". a QUESTION is the
# group of assets that share (input_id, predicate, direction, aspect);
# its labelled answers are the group's outputs.
#
# spec under test (pure functions only; NodeNorm / Retriever calls live
# in the CLI and are exercised by running it):
#   - asset_direction / asset_aspect read the Biolink object direction
#     and aspect qualifiers from an asset; "" and absent both mean None.
#   - question_key groups assets by (input_id, predicate, direction,
#     aspect).
#   - clean_input_name strips a trailing " (human)" and surrounding
#     whitespace; pick_question_name prefers a documented override,
#     otherwise the most frequent cleaned name (ties: shortest, then
#     alphabetical).
#   - answer_category_for: treats -> ChemicalEntity; affects with a
#     gene pinned -> ChemicalEntity; affects with a chemical pinned ->
#     Gene.
#   - template_question renders the fixed English template per
#     question type.
#   - gold_query_graph orients the edge the way the graph stores it
#     (drug treats disease; chemical affects gene) and adds exactly one
#     qualifier set when the question has a direction.
#   - is_positive: TopAnswer and Acceptable only.
#   - is_usable: a question needs at least one labelled answer whose
#     output category is the question's answer category (or unknown);
#     otherwise no pick the pipeline can make is ever labelled.

from __future__ import annotations

from typing import Any

from pipeline.code.translator_assets import (
    EXCLUDED_INPUTS,
    NAME_OVERRIDES,
    answer_category_for,
    asset_aspect,
    asset_direction,
    clean_input_name,
    gold_query_graph,
    is_positive,
    is_usable,
    pick_question_name,
    question_key,
    template_question,
)


def _asset(
    *,
    input_id: str = "NCBIGene:1565",
    input_name: str = "CYP2D6",
    predicate: str = "biolink:affects",
    direction: str = "decreased",
    aspect: str = "activity_or_abundance",
    output_id: str = "CHEBI:1",
    output_category: str | None = "biolink:ChemicalEntity",
    expected: str = "TopAnswer",
) -> dict[str, Any]:
    return {
        "id": "Asset_1",
        "input_id": input_id,
        "input_name": input_name,
        "predicate_id": predicate,
        "output_id": output_id,
        "output_name": "x",
        "output_category": output_category,
        "expected_output": expected,
        "qualifiers": [
            {"parameter": "biolink_qualified_predicate", "value": "biolink:causes"},
            {"parameter": "biolink_object_aspect_qualifier", "value": aspect},
            {"parameter": "biolink_object_direction_qualifier", "value": direction},
        ],
    }


def test_direction_and_aspect_are_read_from_qualifiers() -> None:
    asset = _asset(direction="increased", aspect="abundance")
    assert asset_direction(asset) == "increased"
    assert asset_aspect(asset) == "abundance"


def test_empty_qualifier_values_mean_none() -> None:
    # treats assets carry the three qualifier parameters with "" values.
    asset = _asset(predicate="biolink:treats", direction="", aspect="")
    assert asset_direction(asset) is None
    assert asset_aspect(asset) is None
    no_qualifiers = {**_asset(), "qualifiers": []}
    assert asset_direction(no_qualifiers) is None
    assert asset_aspect(no_qualifiers) is None


def test_question_key_separates_directions() -> None:
    decreased = question_key(_asset(direction="decreased"))
    increased = question_key(_asset(direction="increased"))
    assert decreased == ("NCBIGene:1565", "biolink:affects", "decreased", "activity_or_abundance")
    assert decreased != increased


def test_clean_input_name_strips_species_suffix() -> None:
    assert clean_input_name("BRAF (human)") == "BRAF"
    assert clean_input_name("  CFTR (Human) ") == "CFTR"
    assert clean_input_name("Multiple Sclerosis") == "Multiple Sclerosis"


def test_pick_question_name_prefers_override_then_frequency() -> None:
    override_id = next(iter(NAME_OVERRIDES))
    assert pick_question_name(override_id, ["whatever"]) == NAME_OVERRIDES[override_id]
    assert pick_question_name("MONDO:1", ["Cerebral Palsy", "Cerebral palsy", "Cerebral palsy"]) == "Cerebral palsy"
    # tie on frequency: the shorter name wins, then alphabetical.
    assert pick_question_name("MONDO:2", ["Neuropathy Type", "Neuropathy"]) == "Neuropathy"
    assert pick_question_name("MONDO:3", ["b name", "a name"]) == "a name"


def test_answer_category_for() -> None:
    assert answer_category_for("biolink:treats", pinned_is_gene=False) == "biolink:ChemicalEntity"
    assert answer_category_for("biolink:affects", pinned_is_gene=True) == "biolink:ChemicalEntity"
    assert answer_category_for("biolink:affects", pinned_is_gene=False) == "biolink:Gene"


def test_template_question_per_type() -> None:
    assert template_question(
        "biolink:treats", pinned_is_gene=False, name="Multiple Sclerosis",
        direction=None, aspect=None,
    ) == "What drugs may treat Multiple Sclerosis?"
    assert template_question(
        "biolink:affects", pinned_is_gene=True, name="CYP2D6",
        direction="decreased", aspect="activity_or_abundance",
    ) == "What chemicals may decrease the activity or abundance of CYP2D6?"
    assert template_question(
        "biolink:affects", pinned_is_gene=False, name="Tamoxifen",
        direction="increased", aspect="activity_or_abundance",
    ) == "Which genes may Tamoxifen increase the activity or abundance of?"
    assert template_question(
        "biolink:affects", pinned_is_gene=False, name="Pseudoephedrine",
        direction="decreased", aspect="abundance",
    ) == "Which genes may Pseudoephedrine decrease the abundance of?"


def test_gold_query_graph_treats_points_drug_to_disease() -> None:
    graph = gold_query_graph(
        pinned_curie="MONDO:0005301", pinned_category="biolink:Disease",
        answer_category="biolink:ChemicalEntity", predicate="biolink:treats",
        pinned_is_gene=False, direction=None, aspect=None,
    )
    assert graph == {
        "nodes": {
            "n0": {"ids": ["MONDO:0005301"], "categories": ["biolink:Disease"]},
            "n1": {"categories": ["biolink:ChemicalEntity"]},
        },
        "edges": {"e0": {"subject": "n1", "object": "n0", "predicates": ["biolink:treats"]}},
    }


def test_gold_query_graph_affects_points_chemical_to_gene_with_qualifiers() -> None:
    qualifier_set = [
        {"qualifier_type_id": "biolink:qualified_predicate", "qualifier_value": "biolink:causes"},
        {"qualifier_type_id": "biolink:object_aspect_qualifier", "qualifier_value": "activity_or_abundance"},
        {"qualifier_type_id": "biolink:object_direction_qualifier", "qualifier_value": "decreased"},
    ]
    gene_pinned = gold_query_graph(
        pinned_curie="NCBIGene:1565", pinned_category="biolink:Gene",
        answer_category="biolink:ChemicalEntity", predicate="biolink:affects",
        pinned_is_gene=True, direction="decreased", aspect="activity_or_abundance",
    )
    assert gene_pinned["edges"]["e0"] == {
        "subject": "n1", "object": "n0", "predicates": ["biolink:affects"],
        "qualifier_constraints": [{"qualifier_set": qualifier_set}],
    }
    chemical_pinned = gold_query_graph(
        pinned_curie="CHEBI:41774", pinned_category="biolink:SmallMolecule",
        answer_category="biolink:Gene", predicate="biolink:affects",
        pinned_is_gene=False, direction="decreased", aspect="activity_or_abundance",
    )
    assert chemical_pinned["edges"]["e0"]["subject"] == "n0"
    assert chemical_pinned["edges"]["e0"]["object"] == "n1"


def test_is_positive() -> None:
    assert is_positive("TopAnswer") and is_positive("Acceptable")
    for label in ("NeverShow", "BadButForgivable", "OverlyGeneric", "Performance"):
        assert not is_positive(label)


def test_is_usable_needs_one_answer_in_the_answer_category() -> None:
    chemical_answers = [{"output_category": "biolink:ChemicalEntity"}]
    disease_answers = [{"output_category": "biolink:Disease"}]
    unknown_answers = [{"output_category": None}]
    assert is_usable(chemical_answers, "biolink:ChemicalEntity")
    assert is_usable(unknown_answers, "biolink:Gene")
    assert not is_usable(disease_answers, "biolink:Gene")
    assert not is_usable([], "biolink:Gene")


def test_documented_data_fixes() -> None:
    # the two broken gold sides found in the 2026-09-28 paraphrase review.
    assert NAME_OVERRIDES["MONDO:0017314"] == "vascular Ehlers-Danlos syndrome"
    assert "Kesium" in EXCLUDED_INPUTS["RXCUI:151392"]
    assert "RXCUI:151392" not in NAME_OVERRIDES
