# arax_reasoning.py: turns an ARAX TRAPI response into answers, the
# evidence behind each answer, the reasoning paths from the answer to
# the question's entity, and the node-link graph the UI draws.
#
# fixture: tests/fixtures/arax_ms_trimmed.json, a real ARAX reply to
# "what may treat multiple sclerosis" (MONDO:0005301, knowledge_type
# inferred, 2026-09-28) trimmed to three answers: Fingolimod (rank 1),
# Baclofen, Glatiramer (a gene-mediated prediction).
#
# spec under test:
#   - answers_from_response: one AraxAnswer per result, in ARAX's own
#     order (rank 1..n), for the query node that carries no ids; score
#     from the first analysis; the edges bound to the query edge; the
#     analysis-level support graphs.
#   - support_edges: every edge reachable from the bound edges and the
#     analysis support graphs through biolink:support_graphs,
#     recursively, each once, in breadth-first order. evidence_edges is
#     the same list without the bound edges, which are ARAX's
#     conclusion, not its evidence.
#   - answer_paths: simple paths (node id lists) from the answer to the
#     target over the evidence edges, direction ignored, at most
#     max_hops edges, shortest first, ties by node ids, capped at limit.
#   - reasoning_graph: nodes on the chosen paths (roles query / answer /
#     intermediate), every evidence edge joining two consecutive nodes
#     of a chosen path (with the answers it supports), and the paths.
#   - an empty or malformed response yields no answers.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.code.arax_client import progress_words
from pipeline.code.arax_reasoning import (
    answer_paths,
    answers_from_response,
    ask_arax_to_reason,
    display_category,
    evidence_edges,
    fact_evidence,
    number_facts,
    reasoning_graph,
    render_answers_table,
    render_facts_block,
    support_edges,
)

FIXTURE = Path(__file__).parent / "fixtures" / "arax_ms_trimmed.json"
MS = "MONDO:0005301"
FINGOLIMOD = "CHEBI:63115"
BACLOFEN = "CHEBI:187893"
GLATIRAMER = "DRUGBANK:DB05259"
RRMS = "MONDO:0005314"


def _body() -> dict[str, Any]:
    body: dict[str, Any] = json.loads(FIXTURE.read_text())
    return body


def test_answers_keep_arax_order_and_scores() -> None:
    answers = answers_from_response(_body())
    assert [(a.rank, a.curie, a.name) for a in answers] == [
        (1, FINGOLIMOD, "Fingolimod"),
        (2, BACLOFEN, "Baclofen"),
        (3, GLATIRAMER, "Glatiramer"),
    ]
    assert [round(a.score or 0.0, 3) for a in answers] == [0.998, 0.978, 0.961]
    assert len(answers[0].bound_edge_ids) == 2
    assert len(answers[1].bound_edge_ids) == 1


def test_support_and_evidence_edges() -> None:
    body = _body()
    fingolimod, baclofen, glatiramer = answers_from_response(body)
    assert len(support_edges(body, fingolimod)) == 17
    assert len(evidence_edges(body, fingolimod)) == 15
    assert len(evidence_edges(body, baclofen)) == 6
    assert len(evidence_edges(body, glatiramer)) == 59
    bound = set(fingolimod.bound_edge_ids)
    assert not bound & set(evidence_edges(body, fingolimod))
    assert support_edges(body, fingolimod)[: len(bound)] == fingolimod.bound_edge_ids


def test_paths_are_shortest_first_and_capped() -> None:
    body = _body()
    _, baclofen, glatiramer = answers_from_response(body)
    assert answer_paths(body, baclofen, MS, max_hops=4, limit=10) == [
        [BACLOFEN, MS],
        [BACLOFEN, RRMS, MS],
    ]
    glatiramer_paths = answer_paths(body, glatiramer, MS, max_hops=4, limit=100)
    assert len(glatiramer_paths) == 25
    assert glatiramer_paths[0] == [GLATIRAMER, MS]
    assert [len(p) for p in glatiramer_paths] == sorted(len(p) for p in glatiramer_paths)
    assert answer_paths(body, glatiramer, MS, max_hops=4, limit=3) == glatiramer_paths[:3]
    assert answer_paths(body, glatiramer, MS, max_hops=1, limit=100) == [[GLATIRAMER, MS]]


def test_reasoning_graph_holds_only_path_nodes_and_their_edges() -> None:
    body = _body()
    answers = answers_from_response(body)
    graph = reasoning_graph(body, answers[:2], MS, paths_per_answer=2)
    roles = {node["id"]: node["role"] for node in graph["nodes"]}
    assert roles == {
        MS: "query", FINGOLIMOD: "answer", BACLOFEN: "answer", RRMS: "intermediate",
    }
    assert graph["paths"] == {
        FINGOLIMOD: [[FINGOLIMOD, MS], [FINGOLIMOD, RRMS, MS]],
        BACLOFEN: [[BACLOFEN, MS], [BACLOFEN, RRMS, MS]],
    }
    pairs = {frozenset((e["source"], e["target"])) for e in graph["edges"]}
    assert pairs == {
        frozenset((FINGOLIMOD, MS)), frozenset((FINGOLIMOD, RRMS)),
        frozenset((BACLOFEN, MS)), frozenset((BACLOFEN, RRMS)), frozenset((RRMS, MS)),
    }
    # the RRMS -> MS subclass step is shared by both answers' paths.
    shared = [e for e in graph["edges"] if {e["source"], e["target"]} == {RRMS, MS}]
    assert shared and all(set(e["answers"]) == {FINGOLIMOD, BACLOFEN} for e in shared)
    for edge in graph["edges"]:
        assert edge["predicate"].startswith("biolink:")
        assert edge["id"] in body["message"]["knowledge_graph"]["edges"]


def test_answers_table_lists_rank_score_and_a_path() -> None:
    body = _body()
    table = render_answers_table(body, answers_from_response(body), MS, paths_per_answer=1)
    lines = table.splitlines()
    first = next(line for line in lines if line.startswith("1 |"))
    assert "Fingolimod (CHEBI:63115)" in first
    assert "score=0.998" in first
    assert "path: Fingolimod -" in first
    assert any(line.startswith("3 | Glatiramer (DRUGBANK:DB05259)") for line in lines)


def test_empty_or_malformed_response() -> None:
    assert answers_from_response({}) == []
    assert answers_from_response({"message": {"results": None}}) == []
    body = _body()
    answer = answers_from_response(body)[0]
    assert answer_paths(body, answer, "MONDO:0000001", max_hops=4, limit=3) == []


# ---- numbered facts for the explanation
#
# spec: number_facts gives every edge of a reasoning graph a stable
# fact id F1..Fn in the graph's own edge order and records, per answer
# and per path, the fact ids of each step (all parallel facts joining
# the step's two nodes, strongest first). render_facts_block writes
# the list the explainer cites from: one line per fact with its
# evidence in plain words, then each answer's paths as the chain of
# entities followed by the steps' fact ids.


def test_number_facts_and_block() -> None:
    body = _body()
    answers = answers_from_response(body)
    graph = number_facts(reasoning_graph(body, answers[1:2], MS, paths_per_answer=2))
    fact_ids = [edge["fact_id"] for edge in graph["edges"]]
    assert fact_ids == [f"F{i}" for i in range(1, len(graph["edges"]) + 1)]
    steps = graph["path_facts"][BACLOFEN]
    assert len(steps) == 2
    assert len(steps[0]) == 1 and len(steps[1]) == 2
    by_id = {edge["fact_id"]: edge for edge in graph["edges"]}
    assert all({by_id[f]["source"], by_id[f]["target"]} == {BACLOFEN, MS} for f in steps[0][0])
    assert all({by_id[f]["source"], by_id[f]["target"]} == {RRMS, MS} for f in steps[1][1])

    block = render_facts_block(graph)
    assert block.startswith("Reasoning facts")
    assert "F1 | Baclofen --" in block
    assert "Baclofen (CHEBI:187893):" in block
    assert "path 2: Baclofen -> relapsing-remitting multiple sclerosis -> multiple sclerosis" in block
    assert "facts: [" in block and "] -> [" in block
    # evidence levels are spelled out, never printed as enum values.
    assert "knowledge_assertion" not in block and "kl=" not in block


# ---- fact evidence
#
# spec: fact_evidence reads what a fact rests on from its attributes and
# sources: approval status in words, the most advanced research phase,
# FDA application numbers, DailyMed labels as links, registered trials
# (most advanced, then largest, first; capped, with the total kept),
# PMIDs (capped, total kept), ARAX's normalized co-mention distance
# ("inf" means never co-mentioned and is dropped), the supporting data
# sources in readable names and the primary source's record URL.


def test_fact_evidence_reads_trials_approvals_labels_and_comention() -> None:
    edge = {
        "subject": "CHEBI:1", "object": "MONDO:1", "predicate": "biolink:treats",
        "sources": [
            {"resource_id": "infores:multiomics-clinicaltrials", "resource_role": "primary_knowledge_source",
             "source_record_urls": ["https://example.org/record/1"]},
            {"resource_id": "infores:aact", "resource_role": "supporting_data_source"},
            {"resource_id": "infores:arax", "resource_role": "aggregator_knowledge_source"},
        ],
        "attributes": [
            {"attribute_type_id": "biolink:clinical_approval_status", "value": "approved_for_condition"},
            {"attribute_type_id": "biolink:max_research_phase", "value": "clinical_trial_phase_4"},
            {"attribute_type_id": "biolink:FDA_regulatory_approvals", "value": ["NDA022527"]},
            {"attribute_type_id": "biolink:publications",
             "value": ["dailymed:abc-123", "PMID:111", "PMID:222"]},
            {"attribute_type_id": "biolink:has_supporting_studies", "value": {
                "CLINICALTRIALS:NCT0001": {"id": "CLINICALTRIALS:NCT0001", "name": "small",
                                           "clinical_trial_phase": "clinical_trial_phase_2",
                                           "clinical_trial_overall_status": "COMPLETED",
                                           "clinical_trial_enrollment": 40},
                "CLINICALTRIALS:NCT0002": {"id": "CLINICALTRIALS:NCT0002", "name": "big",
                                           "clinical_trial_phase": "clinical_trial_phase_4",
                                           "clinical_trial_overall_status": "ACTIVE_NOT_RECRUITING",
                                           "clinical_trial_enrollment": 900},
            }},
            {"attribute_type_id": "EDAM-DATA:2526", "value": "0.41"},
        ],
    }
    evidence = fact_evidence(edge)
    assert evidence["approval"] == "approved for this condition"
    assert evidence["max_phase"] == "4"
    assert evidence["fda_approvals"] == ["NDA022527"]
    assert evidence["labels"] == [{
        "id": "dailymed:abc-123",
        "url": "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=abc-123",
    }]
    assert [t["id"] for t in evidence["trials"]] == ["NCT0002", "NCT0001"]
    assert evidence["trials"][0]["status"] == "active not recruiting"
    assert evidence["trials"][0]["url"] == "https://clinicaltrials.gov/study/NCT0002"
    assert evidence["n_trials"] == 2
    assert evidence["pmids"] == ["PMID:111", "PMID:222"] and evidence["n_pmids"] == 2
    assert evidence["ngd"] == 0.41
    assert evidence["via"] == ["AACT"]
    assert evidence["record_url"] == "https://example.org/record/1"


def test_fact_evidence_of_a_bare_fact_is_empty() -> None:
    edge = {"subject": "A", "object": "B", "predicate": "biolink:related_to",
            "attributes": [{"attribute_type_id": "EDAM-DATA:2526", "value": "inf"}]}
    evidence = fact_evidence(edge)
    assert evidence["approval"] is None and evidence["max_phase"] is None
    assert evidence["trials"] == [] and evidence["n_trials"] == 0
    assert evidence["ngd"] is None and evidence["via"] == [] and evidence["record_url"] is None


# ---- display category
#
# spec: ARAX lists a node's categories unordered, mixins and ancestors
# first (["biolink:PhysicalEssence", ..., "biolink:Drug"]). the UI
# colours a card by its kind, so display_category picks the most
# specific known kind present (drug before chemical, disease before
# phenotype, gene before protein ...); an unknown list keeps its first
# entry; an empty one gives None.


def test_display_category_prefers_the_specific_kind() -> None:
    assert display_category([
        "biolink:PhysicalEssence", "biolink:PhysicalEssenceOrOccurrent",
        "biolink:ChemicalEntity", "biolink:NamedThing", "biolink:Drug",
    ]) == "biolink:Drug"
    assert display_category([
        "biolink:ThingWithTaxon", "biolink:BiologicalEntity", "biolink:Disease",
    ]) == "biolink:Disease"
    assert display_category([
        "biolink:BiologicalEntity", "biolink:ChemicalEntityOrGeneOrGeneProduct", "biolink:Gene",
    ]) == "biolink:Gene"
    assert display_category(["biolink:Mystery", "biolink:Other"]) == "biolink:Mystery"
    assert display_category([]) is None


def test_graph_nodes_use_display_category() -> None:
    body = _body()
    answers = answers_from_response(body)
    assert answers[2].category == "biolink:ChemicalEntity"
    graph = reasoning_graph(body, answers[:1], MS, paths_per_answer=1)
    assert {n["id"]: n["category"] for n in graph["nodes"]}[MS] == "biolink:Disease"


# ---- lookup answers
#
# spec: when ARAX only looks facts up (no knowledge_type "inferred"),
# the edges bound to an answer are stored facts with no support graphs
# of their own. they ARE the evidence and must count as such; only a
# bound edge that carries support graphs is ARAX's own conclusion and
# is left out as circular.


def _lookup_body() -> dict[str, Any]:
    return {
        "message": {
            "query_graph": {
                "nodes": {"n0": {"categories": ["biolink:Drug"]}, "n1": {"ids": ["MONDO:1"]}},
                "edges": {"e0": {"subject": "n0", "object": "n1", "predicates": ["biolink:treats"]}},
            },
            "knowledge_graph": {
                "nodes": {
                    "CHEBI:1": {"name": "levodopa", "categories": ["biolink:Drug"]},
                    "MONDO:1": {"name": "Parkinson disease", "categories": ["biolink:Disease"]},
                },
                "edges": {
                    "stored": {
                        "subject": "CHEBI:1", "object": "MONDO:1", "predicate": "biolink:treats",
                        "sources": [{"resource_id": "infores:drugcentral",
                                     "resource_role": "primary_knowledge_source"}],
                        "attributes": [],
                    },
                },
            },
            "auxiliary_graphs": {},
            "results": [{
                "node_bindings": {"n0": [{"id": "CHEBI:1"}], "n1": [{"id": "MONDO:1"}]},
                "analyses": [{"edge_bindings": {"e0": [{"id": "stored"}]}, "score": 0.9}],
            }],
        },
    }


def test_lookup_bound_edges_are_evidence() -> None:
    body = _lookup_body()
    (answer,) = answers_from_response(body)
    assert answer.curie == "CHEBI:1"
    assert evidence_edges(body, answer) == ["stored"]
    assert answer_paths(body, answer, "MONDO:1", max_hops=4, limit=3) == [["CHEBI:1", "MONDO:1"]]


# ---- asking ARAX to reason
#
# spec: for "what treats <disease>" (a treats-family predicate between an
# unpinned chemical and a pinned disease or phenotype) and for "what
# raises / lowers <gene>" (biolink:affects with qualifier constraints
# between a chemical and a gene), ask_arax_to_reason returns the request
# ARAX reasons on: predicate biolink:treats, the chemical as subject, an
# unpinned chemical answer as biolink:ChemicalEntity, knowledge_type
# inferred, and says what it changed. anything else, and a request
# already in that form, comes back unchanged with no changes listed.


def _query(subject: str, obj: str, predicate: str, nodes: dict[str, Any], **edge: Any) -> dict[str, Any]:
    return {"message": {"query_graph": {
        "nodes": nodes,
        "edges": {"e0": {"subject": subject, "object": obj, "predicates": [predicate], **edge}},
    }}}


RA_NODES = {
    "n0": {"ids": ["MONDO:0008383"], "categories": ["biolink:Disease"]},
    "n1": {"categories": ["biolink:Drug"]},
}


def test_ask_arax_to_reason_turns_a_reversed_treats_lookup_into_inference() -> None:
    sent, changes = ask_arax_to_reason(_query("n0", "n1", "biolink:treats", RA_NODES))
    graph = sent["message"]["query_graph"]
    assert graph["edges"]["e0"] == {
        "subject": "n1", "object": "n0", "predicates": ["biolink:treats"], "knowledge_type": "inferred",
    }
    assert graph["nodes"]["n1"]["categories"] == ["biolink:ChemicalEntity"]
    assert graph["nodes"]["n0"] == RA_NODES["n0"]
    assert changes == [
        "edge turned so the chemical is the subject",
        "answer category set to biolink:ChemicalEntity",
        "knowledge_type set to inferred",
    ]


def test_ask_arax_to_reason_narrows_the_treats_family_to_treats() -> None:
    sent, changes = ask_arax_to_reason(
        _query("n1", "n0", "biolink:treats_or_applied_or_studied_to_treat", RA_NODES),
    )
    assert sent["message"]["query_graph"]["edges"]["e0"]["predicates"] == ["biolink:treats"]
    assert "predicate set to biolink:treats" in changes


def test_ask_arax_to_reason_infers_gene_regulation_with_qualifiers() -> None:
    nodes = {
        "n0": {"ids": ["NCBIGene:1565"], "categories": ["biolink:Gene"]},
        "n1": {"categories": ["biolink:SmallMolecule"]},
    }
    qualifiers = [{"qualifier_set": [{"qualifier_type_id": "biolink:object_direction_qualifier",
                                      "qualifier_value": "decreased"}]}]
    sent, changes = ask_arax_to_reason(
        _query("n1", "n0", "biolink:affects", nodes, qualifier_constraints=qualifiers),
    )
    edge = sent["message"]["query_graph"]["edges"]["e0"]
    assert edge["knowledge_type"] == "inferred" and edge["subject"] == "n1"
    assert sent["message"]["query_graph"]["nodes"]["n1"]["categories"] == ["biolink:ChemicalEntity"]
    assert changes[-1] == "knowledge_type set to inferred"


def test_ask_arax_to_reason_leaves_other_questions_and_ready_requests_alone() -> None:
    trials = _query("n1", "n0", "biolink:in_clinical_trials_for", RA_NODES)
    assert ask_arax_to_reason(trials) == (trials, [])
    genes = _query("n1", "n0", "biolink:gene_associated_with_condition", {
        "n0": {"ids": ["MONDO:0008383"], "categories": ["biolink:Disease"]},
        "n1": {"categories": ["biolink:Gene"]},
    })
    assert ask_arax_to_reason(genes) == (genes, [])
    ready = _query("n1", "n0", "biolink:treats", {
        "n0": RA_NODES["n0"], "n1": {"categories": ["biolink:ChemicalEntity"]},
    }, knowledge_type="inferred")
    assert ask_arax_to_reason(ready) == (ready, [])


# ---- ARAX progress in plain words
#
# spec: progress_words turns the steps of ARAX's progress log a reader
# cares about into plain words, numbers kept, and drops bookkeeping.


def test_progress_words() -> None:
    assert progress_words(
        "Calling XDTD from Expand for qedge e0 (has knowledge_type == inferred)"
    ) == "Predicting treatments with ARAX's drug-repurposing model (xDTD)"
    assert progress_words(
        "After Expand, the KG has 2417 nodes and 8880 edges (creative_DTD_qedge_0: 555)"
    ) == "Gathered 2417 entities and 8880 facts"
    assert progress_words("Expanding qedge e0 using infores:retriever") == (
        "Looking facts up in the Tier 0 graph through Retriever"
    )
    assert progress_words("Processing is complete and resulted in 475 results.") == "Done: 475 ranked answers"
    assert progress_words("Adding a QueryNode to Message with input parameters {'key': 'x'}") is None
    assert progress_words("Storing result in the cache") is None
