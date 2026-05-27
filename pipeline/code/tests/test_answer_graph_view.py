# Stage 13: _build_answer_graph_view.
#
# the function takes the pinned entity + the LLM's picked answer CURIEs
# + PloverDB's knowledge_graph response, and produces a structured graph
# view suitable for rendering as a node-link diagram with hover-able
# evidence on the edges. it's pure (no IO, no LLM), so we test it
# strictly against fabricated PloverDB responses.
#
# spec (what the function must do):
#   1. emit `pinned_node` with curie/label/category/role="pinned"
#   2. emit `answer_nodes` — one per picked CURIE, role="answer".
#      labels/categories come from the PloverDB knowledge_graph.nodes
#      block. unknown CURIEs (not in KG) still get a node but with
#      label=None and category=None — we never drop a picked answer.
#   3. emit `edges` — only edges that touch BOTH the pinned node AND
#      one of the picked answer nodes. edges between two non-answer
#      nodes or two pinned-irrelevant nodes are dropped.
#   4. for each kept edge, extract these attributes from its
#      attributes[] list (TRAPI 1.5 format):
#        - biolink:knowledge_level         → knowledge_level (str or None)
#        - biolink:primary_knowledge_source → primary_knowledge_source
#        - biolink:publications            → supporting_publications (list)
#        - biolink:supporting_text         → supporting_text_snippets
#          (list of {pmid, date, sentence}; flattens the per-pmid dict)
#   5. malformed / missing attributes degrade gracefully: the field
#      becomes None / empty list — the function MUST NOT raise.
#   6. an empty answer list → empty answer_nodes + empty edges, but
#      pinned_node is still emitted.

from code.pipeline import _build_answer_graph_view


# ---- a minimal but realistic fabricated TRAPI response ----

def _minimal_plover_kg():
    # 1 pinned node (T2DM), 2 candidate drugs (metformin picked, aspirin not picked),
    # 1 unrelated edge (aspirin → some-other-disease) that should be dropped.
    return {
        "message": {
            "knowledge_graph": {
                "nodes": {
                    "MONDO:0005148": {
                        "name": "type 2 diabetes mellitus",
                        "categories": ["biolink:Disease"],
                    },
                    "CHEBI:6801": {
                        "name": "metformin",
                        "categories": ["biolink:Drug"],
                    },
                    "CHEBI:15365": {
                        "name": "aspirin",
                        "categories": ["biolink:Drug"],
                    },
                },
                "edges": {
                    # the canonical metformin→T2DM treats edge with full evidence
                    "edge1": {
                        "subject": "CHEBI:6801",
                        "object": "MONDO:0005148",
                        "predicate": "biolink:treats",
                        "attributes": [
                            {
                                "attribute_type_id": "biolink:knowledge_level",
                                "value": "knowledge_assertion",
                            },
                            {
                                "attribute_type_id": "biolink:agent_type",
                                "value": "manual_agent",
                            },
                            {
                                "attribute_type_id": "biolink:primary_knowledge_source",
                                "value": "infores:drugcentral",
                            },
                            {
                                "attribute_type_id": "biolink:publications",
                                "value": ["PMID:12345", "PMID:67890"],
                            },
                            {
                                "attribute_type_id": "biolink:supporting_text",
                                "value": {
                                    "PMID:12345": {
                                        "publication date": "2018 Mar",
                                        "sentence": "Metformin is recommended as first-line therapy for type 2 diabetes.",
                                    },
                                },
                            },
                        ],
                    },
                    # aspirin → some unrelated disease — must be dropped because
                    # neither endpoint is the pinned node + a picked answer
                    "edge2": {
                        "subject": "CHEBI:15365",
                        "object": "MONDO:0009999",
                        "predicate": "biolink:treats",
                        "attributes": [],
                    },
                },
            }
        }
    }


# ---- happy path: typical answer with one picked drug ----

def test_basic_shape_correct():
    # pinned = T2DM, one picked answer (metformin). expect:
    # pinned_node + 1 answer_node + 1 edge (the metformin→T2DM one).
    view = _build_answer_graph_view(
        pinned_curie="MONDO:0005148",
        pinned_label="type 2 diabetes mellitus",
        pinned_category="biolink:Disease",
        picked_answer_curies=["CHEBI:6801"],
        plover_response=_minimal_plover_kg(),
    )
    # pinned
    assert view["pinned_node"]["curie"] == "MONDO:0005148"
    assert view["pinned_node"]["label"] == "type 2 diabetes mellitus"
    assert view["pinned_node"]["category"] == "biolink:Disease"
    assert view["pinned_node"]["role"] == "pinned"
    # answers
    assert len(view["answer_nodes"]) == 1
    a = view["answer_nodes"][0]
    assert a["curie"] == "CHEBI:6801"
    assert a["label"] == "metformin"
    assert a["category"] == "biolink:Drug"
    assert a["role"] == "answer"
    # edges
    assert len(view["edges"]) == 1
    e = view["edges"][0]
    assert e["source"] == "CHEBI:6801"
    assert e["target"] == "MONDO:0005148"
    assert e["predicate"] == "biolink:treats"


def test_edge_evidence_attributes_extracted():
    # the heart of "research-grade" — every PloverDB edge attribute we
    # care about must surface in the edge object, exactly as found.
    view = _build_answer_graph_view(
        pinned_curie="MONDO:0005148",
        pinned_label="type 2 diabetes mellitus",
        pinned_category="biolink:Disease",
        picked_answer_curies=["CHEBI:6801"],
        plover_response=_minimal_plover_kg(),
    )
    e = view["edges"][0]
    assert e["knowledge_level"] == "knowledge_assertion"
    assert e["agent_type"] == "manual_agent"
    assert e["primary_knowledge_source"] == "infores:drugcentral"
    assert e["supporting_publications"] == ["PMID:12345", "PMID:67890"]
    # supporting_text is dict-of-dict in TRAPI; the view flattens to a list
    # of {pmid, date, sentence} for easier rendering.
    assert e["supporting_text_snippets"] == [
        {
            "pmid": "PMID:12345",
            "date": "2018 Mar",
            "sentence": "Metformin is recommended as first-line therapy for type 2 diabetes.",
        }
    ]


def test_irrelevant_edges_are_dropped():
    # the aspirin→MONDO:0009999 edge in the fixture is between two nodes
    # that aren't in the (pinned, picked) set. it must NOT appear in the
    # output even though it's in the PloverDB response.
    view = _build_answer_graph_view(
        pinned_curie="MONDO:0005148",
        pinned_label="type 2 diabetes mellitus",
        pinned_category="biolink:Disease",
        picked_answer_curies=["CHEBI:6801"],
        plover_response=_minimal_plover_kg(),
    )
    edge_ids = [e["id"] for e in view["edges"]]
    assert "edge1" in edge_ids
    assert "edge2" not in edge_ids


# ---- degraded inputs ----

def test_picked_curie_not_in_kg_still_emits_a_node():
    # an LLM could pick a CURIE that's not in the PloverDB response
    # (parsing slip, hallucination). we must STILL emit it as a node,
    # with label/category=None — never silently drop a picked answer.
    view = _build_answer_graph_view(
        pinned_curie="MONDO:0005148",
        pinned_label="type 2 diabetes mellitus",
        pinned_category="biolink:Disease",
        picked_answer_curies=["CHEBI:99999"],   # not in fixture
        plover_response=_minimal_plover_kg(),
    )
    assert len(view["answer_nodes"]) == 1
    a = view["answer_nodes"][0]
    assert a["curie"] == "CHEBI:99999"
    assert a["label"] is None
    assert a["category"] is None
    assert a["role"] == "answer"
    # no edges (no edge connects MONDO:0005148 to CHEBI:99999 in fixture)
    assert view["edges"] == []


def test_edge_with_no_attributes_block_yields_nones():
    # PloverDB edges sometimes have an empty attributes list. all the
    # provenance fields must degrade to None / empty list — not raise.
    kg = _minimal_plover_kg()
    kg["message"]["knowledge_graph"]["edges"]["edge1"]["attributes"] = []
    view = _build_answer_graph_view(
        pinned_curie="MONDO:0005148",
        pinned_label="type 2 diabetes mellitus",
        pinned_category="biolink:Disease",
        picked_answer_curies=["CHEBI:6801"],
        plover_response=kg,
    )
    e = view["edges"][0]
    assert e["knowledge_level"] is None
    assert e["agent_type"] is None
    assert e["primary_knowledge_source"] is None
    assert e["supporting_publications"] == []
    assert e["supporting_text_snippets"] == []


def test_edge_with_missing_attributes_key_yields_nones():
    # even more degenerate: the attributes key isn't present at all
    # (older TRAPI snapshots / mock responses). must still be safe.
    kg = _minimal_plover_kg()
    del kg["message"]["knowledge_graph"]["edges"]["edge1"]["attributes"]
    view = _build_answer_graph_view(
        pinned_curie="MONDO:0005148",
        pinned_label="type 2 diabetes mellitus",
        pinned_category="biolink:Disease",
        picked_answer_curies=["CHEBI:6801"],
        plover_response=kg,
    )
    e = view["edges"][0]
    assert e["knowledge_level"] is None
    assert e["supporting_publications"] == []


def test_empty_plover_response_does_not_raise():
    # PloverDB returns {} when the query produced no results. function
    # must emit pinned_node, empty answers, empty edges — not raise.
    view = _build_answer_graph_view(
        pinned_curie="MONDO:0005148",
        pinned_label="type 2 diabetes mellitus",
        pinned_category="biolink:Disease",
        picked_answer_curies=[],
        plover_response={},
    )
    assert view["pinned_node"]["curie"] == "MONDO:0005148"
    assert view["answer_nodes"] == []
    assert view["edges"] == []


def test_picked_curies_empty_emits_pinned_only():
    # Stage 11 picked nothing — we still want the pinned node so the
    # frontend can render "we queried X but got nothing back".
    view = _build_answer_graph_view(
        pinned_curie="MONDO:0005148",
        pinned_label="type 2 diabetes mellitus",
        pinned_category="biolink:Disease",
        picked_answer_curies=[],
        plover_response=_minimal_plover_kg(),
    )
    assert view["pinned_node"]["role"] == "pinned"
    assert view["answer_nodes"] == []
    assert view["edges"] == []


# ---- edge direction invariants ----

def test_edge_kept_regardless_of_subject_object_orientation():
    # TRAPI edges can put the pinned node in either subject or object
    # position depending on the predicate direction. the view must keep
    # the edge either way — and faithfully report source/target as
    # PloverDB had them (don't silently flip).
    kg = _minimal_plover_kg()
    # flip the edge to be MONDO:0005148 → CHEBI:6801
    kg["message"]["knowledge_graph"]["edges"]["edge1"] = {
        "subject": "MONDO:0005148",
        "object": "CHEBI:6801",
        "predicate": "biolink:treated_by",
        "attributes": [],
    }
    view = _build_answer_graph_view(
        pinned_curie="MONDO:0005148",
        pinned_label="type 2 diabetes mellitus",
        pinned_category="biolink:Disease",
        picked_answer_curies=["CHEBI:6801"],
        plover_response=kg,
    )
    assert len(view["edges"]) == 1
    e = view["edges"][0]
    # preserved as-was (NOT flipped)
    assert e["source"] == "MONDO:0005148"
    assert e["target"] == "CHEBI:6801"
    assert e["predicate"] == "biolink:treated_by"


# ---- multiple edges between same pair ----

def test_multiple_edges_between_same_pair_all_kept():
    # PloverDB often returns multiple edges between the same node pair —
    # one knowledge_assertion edge + one prediction edge + a hand-curated
    # edge — each with different provenance. we keep ALL of them; the
    # frontend can choose to group or stack them visually.
    kg = _minimal_plover_kg()
    kg["message"]["knowledge_graph"]["edges"]["edge1_alt"] = {
        "subject": "CHEBI:6801",
        "object": "MONDO:0005148",
        "predicate": "biolink:treats",
        "attributes": [
            {
                "attribute_type_id": "biolink:knowledge_level",
                "value": "prediction",  # different provenance from edge1
            },
        ],
    }
    view = _build_answer_graph_view(
        pinned_curie="MONDO:0005148",
        pinned_label="type 2 diabetes mellitus",
        pinned_category="biolink:Disease",
        picked_answer_curies=["CHEBI:6801"],
        plover_response=kg,
    )
    assert len(view["edges"]) == 2
    levels = sorted([e["knowledge_level"] for e in view["edges"]])
    assert levels == ["knowledge_assertion", "prediction"]
