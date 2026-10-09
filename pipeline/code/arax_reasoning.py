# arax_reasoning.py — reads an ARAX TRAPI response. ARAX (the RTX
# team's reasoner) answers "what may treat X" by inference, so an
# answer is not one stored fact but a conclusion backed by evidence:
# the edge bound to the query edge (ARAX's own claim, primary source
# infores:arax) carries biolink:support_graphs, each an auxiliary graph
# of other edges, which may carry support graphs of their own. the
# analysis can add support graphs too (e.g. literature co-occurrence).
#
# this module turns that nesting into what the pipeline and the UI
# need: ranked answers, the evidence edges behind each, the reasoning
# paths from the answer to the question's entity, and a node-link
# graph of those paths. every function is pure (no IO, no logging), so
# the tests run on a saved ARAX reply.

from __future__ import annotations

# collections.deque: stdlib. breadth-first walks over support graphs
# and over candidate paths.
from collections import deque

# itertools.pairwise: stdlib. consecutive node pairs of a path.
from itertools import pairwise

# dataclasses: stdlib. AraxAnswer is built once and only read.
from dataclasses import dataclass
from typing import Any

# the evidence ladders the lookup table already ranks by, reused so
# the fact shown for a reasoning step is chosen the same way here.
from .reduction import AGENT_TYPE_RANK, KNOWLEDGE_LEVEL_RANK, NOT_PROVIDED

SUPPORT_GRAPHS = "biolink:support_graphs"

# reasoning paths longer than this are not drawn or described. four
# edges covers drug -> gene -> gene -> disease with one subtype step,
# which is the longest shape ARAX's drug-treatment model produces.
MAX_PATH_HOPS = 4

# a safety cap on the breadth-first path search. a support graph of a
# few hundred edges can hold millions of 4-edge simple paths; paths
# come out shortest first, so the cap only ever drops the longest.
MAX_EXPLORED_PATHS = 20_000

# publications carried per edge into the UI graph; the full list stays
# in the saved ARAX response.
MAX_PUBLICATIONS_PER_EDGE = 3

# per fact, how many trials and PMIDs travel into the graph (the UI's
# citation cards and the explainer's fact list); the totals are kept.
MAX_TRIALS_PER_FACT = 5
MAX_PMIDS_PER_FACT = 10

# readable names for the Tier 0 sources ARAX's facts come from. a
# source not listed here shows as its infores id without the prefix.
SOURCE_NAMES = {
    "infores:multiomics-clinicaltrials": "ClinicalTrials.gov",
    "infores:multiomics-drugapprovals": "FDA drug labels (DailyMed)",
    "infores:clinicaltrials": "ClinicalTrials.gov",
    "infores:aact": "AACT",
    "infores:dailymed": "DailyMed",
    "infores:faers": "FDA adverse event reports (FAERS)",
    "infores:drugcentral": "DrugCentral",
    "infores:ttd": "Therapeutic Target Database",
    "infores:drug-repurposing-hub": "Drug Repurposing Hub",
    "infores:chembl": "ChEMBL",
    "infores:ctd": "Comparative Toxicogenomics Database",
    "infores:obie": "OBIE",
    "infores:ubergraph": "Ubergraph (ontology)",
    "infores:mondo": "Mondo disease ontology",
    "infores:semmeddb": "SemMedDB (text-mined)",
    "infores:text-mining-provider-targeted": "Text Mining KP (text-mined)",
    "infores:arax": "ARAX literature co-mention (PubMed)",
    "infores:diseases": "DISEASES (Jensen lab)",
    "infores:gtopdb": "Guide to Pharmacology",
    "infores:hmdb": "HMDB",
    "infores:go-cam": "GO-CAM",
    "infores:goa": "Gene Ontology annotations",
    "infores:hpo-annotations": "HPO annotations",
    "infores:disgenet": "DisGeNET",
    "infores:clinvar": "ClinVar",
    "infores:gwas-catalog": "GWAS Catalog",
    "infores:biolink": "Biolink",
}

# Biolink evidence words in plain English, for the explainer's fact
# list and the UI (the explainer must never print the enum values).
KNOWLEDGE_LEVEL_WORDS = {
    "knowledge_assertion": "curated assertion",
    "logical_entailment": "inferred from an ontology",
    "prediction": "prediction",
    "statistical_association": "statistical association",
    "observation": "observation",
    "not_provided": "evidence level not recorded",
}
AGENT_TYPE_WORDS = {
    "manual_agent": "made by a curator",
    "manual_validation_of_automated_agent": "automated, checked by a curator",
    "automated_agent": "made by software",
    "data_analysis_pipeline": "made by an analysis pipeline",
    "computational_model": "made by a computational model",
    "text_mining_agent": "text-mined",
    "image_processing_agent": "image processing",
    "not_provided": "agent not recorded",
}
APPROVAL_WORDS = {
    "approved_for_condition": "approved for this condition",
    "fda_approved_for_condition": "FDA-approved for this condition",
    "not_approved_for_condition": "not approved for this condition",
    "off_label_use": "used off-label",
    "post_approval_withdrawal": "withdrawn after approval",
}


# the kinds the UI colours cards by, most specific first. ARAX lists a
# node's categories unordered, mixins and ancestors first, so its first
# entry ("biolink:PhysicalEssence") says nothing about what the thing is.
DISPLAY_CATEGORIES = tuple(f"biolink:{name}" for name in (
    "Drug", "SmallMolecule", "MolecularMixture", "ChemicalMixture",
    "ComplexMolecularMixture", "ChemicalEntity",
    "Disease", "PhenotypicFeature", "DiseaseOrPhenotypicFeature",
    "Gene", "Protein", "GeneFamily",
    "Pathway", "BiologicalProcess", "MolecularActivity", "CellularComponent",
    "Cell", "GrossAnatomicalStructure", "AnatomicalEntity",
))


# ARAX infers answers for two question shapes when asked with
# knowledge_type "inferred"; these sets say which query nodes fit them.
CHEMICAL_CATEGORIES = frozenset(f"biolink:{name}" for name in (
    "ChemicalEntity", "Drug", "SmallMolecule", "MolecularEntity", "MolecularMixture",
    "ChemicalMixture", "ComplexMolecularMixture",
))
CONDITION_CATEGORIES = frozenset(f"biolink:{name}" for name in (
    "Disease", "DiseaseOrPhenotypicFeature", "PhenotypicFeature",
))
GENE_CATEGORIES = frozenset(f"biolink:{name}" for name in (
    "Gene", "Protein", "GeneOrGeneProduct", "GeneProduct", "Polypeptide",
))
# the treatment predicates a "what treats X" question is built with.
TREATS_FAMILY = frozenset({
    "biolink:treats", "biolink:treats_or_applied_or_studied_to_treat", "biolink:applied_to_treat",
})


def ask_arax_to_reason(message: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    # ARAX reasons (multi-hop, with support graphs) only when the query
    # edge says knowledge_type "inferred", and its treatment inference
    # only runs for a ChemicalEntity answer node: with biolink:Drug the
    # same inferred query ran for over 20 minutes without answering
    # (rheumatoid arthritis, 2026-09-29), with ChemicalEntity it
    # answered in 49 s. Stage 8's prompt asks for both, but models skip
    # it. so for the two shapes ARAX can infer, the request is put in
    # the form ARAX needs:
    #   treats: a chemical treats the pinned disease or phenotype
    #     (predicate biolink:treats, chemical as subject);
    #   affects: a chemical affects a gene with qualifier constraints
    #     (the direction question), chemical as subject.
    # returns the request to send and what was changed, in words; the
    # LLM's own query stays in trapi_query.json.
    graph = (message.get("message") or {}).get("query_graph") or {}
    nodes: dict[str, Any] = graph.get("nodes") or {}
    edges: dict[str, Any] = graph.get("edges") or {}
    if len(edges) != 1 or len(nodes) != 2:
        return message, []
    edge_id, edge = next(iter(edges.items()))
    subject, obj = edge.get("subject"), edge.get("object")
    if subject not in nodes or obj not in nodes:
        return message, []

    def categories(node_id: str) -> set[str]:
        return set(nodes[node_id].get("categories") or [])

    def pinned(node_id: str) -> bool:
        return bool(nodes[node_id].get("ids"))

    predicates = set(edge.get("predicates") or [])
    chemical = next((n for n in (subject, obj) if categories(n) & CHEMICAL_CATEGORIES and not pinned(n)), None)
    condition = next((n for n in (subject, obj) if categories(n) & CONDITION_CATEGORIES and pinned(n)), None)
    gene = next((n for n in (subject, obj) if categories(n) & GENE_CATEGORIES), None)
    chemical_any = next((n for n in (subject, obj) if categories(n) & CHEMICAL_CATEGORIES), None)

    changes: list[str] = []
    new_edge = dict(edge)
    new_nodes = {key: dict(value) for key, value in nodes.items()}
    if predicates & TREATS_FAMILY and chemical and condition:
        if predicates != {"biolink:treats"}:
            new_edge["predicates"] = ["biolink:treats"]
            changes.append("predicate set to biolink:treats")
        if subject != chemical:
            new_edge["subject"], new_edge["object"] = chemical, condition
            changes.append("edge turned so the chemical is the subject")
        answer = chemical
    elif "biolink:affects" in predicates and edge.get("qualifier_constraints") and chemical_any and gene:
        if subject != chemical_any:
            new_edge["subject"], new_edge["object"] = chemical_any, gene
            changes.append("edge turned so the chemical is the subject")
        answer = chemical_any if not pinned(chemical_any) else None
    else:
        return message, []
    if answer is not None and categories(answer) != {"biolink:ChemicalEntity"}:
        new_nodes[answer]["categories"] = ["biolink:ChemicalEntity"]
        changes.append("answer category set to biolink:ChemicalEntity")
    if edge.get("knowledge_type") != "inferred":
        new_edge["knowledge_type"] = "inferred"
        changes.append("knowledge_type set to inferred")
    if not changes:
        return message, []
    return {
        **message,
        "message": {
            **message["message"],
            "query_graph": {**graph, "nodes": new_nodes, "edges": {edge_id: new_edge}},
        },
    }, changes


def display_category(categories: list[Any]) -> str | None:
    present = [str(c) for c in categories]
    for category in DISPLAY_CATEGORIES:
        if category in present:
            return category
    return present[0] if present else None


@dataclass(frozen=True)
class AraxAnswer:
    rank: int                      # 1-based position in ARAX's own order
    curie: str
    name: str
    category: str | None
    score: float | None            # ARAX's score from the first analysis
    bound_edge_ids: list[str]      # edges bound to the query edge (ARAX's claim)
    support_graph_ids: list[str]   # analysis-level auxiliary graphs


def _message(body: dict[str, Any]) -> dict[str, Any]:
    message = body.get("message")
    return message if isinstance(message, dict) else {}


def _kg(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    knowledge_graph = _message(body).get("knowledge_graph") or {}
    nodes = knowledge_graph.get("nodes") or {}
    edges = knowledge_graph.get("edges") or {}
    return (
        nodes if isinstance(nodes, dict) else {},
        edges if isinstance(edges, dict) else {},
    )


def _answer_qnode(message: dict[str, Any]) -> str | None:
    # the query node without ids is the one ARAX filled in.
    nodes = (message.get("query_graph") or {}).get("nodes") or {}
    unpinned = sorted(
        qnode_id for qnode_id, qnode in nodes.items()
        if isinstance(qnode, dict) and not qnode.get("ids")
    )
    return unpinned[0] if unpinned else None


def answers_from_response(body: dict[str, Any]) -> list[AraxAnswer]:
    message = _message(body)
    results = message.get("results")
    qnode = _answer_qnode(message)
    if not isinstance(results, list) or qnode is None:
        return []
    nodes, _ = _kg(body)
    answers: list[AraxAnswer] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        bindings = (result.get("node_bindings") or {}).get(qnode) or []
        if not bindings or not isinstance(bindings[0], dict) or not bindings[0].get("id"):
            continue
        curie = str(bindings[0]["id"])
        analyses = result.get("analyses") or []
        first = analyses[0] if analyses and isinstance(analyses[0], dict) else {}
        bound = [
            str(binding["id"])
            for edge_bindings in (first.get("edge_bindings") or {}).values()
            for binding in edge_bindings
            if isinstance(binding, dict) and binding.get("id")
        ]
        node = nodes.get(curie) or {}
        score = first.get("score")
        answers.append(AraxAnswer(
            rank=len(answers) + 1,
            curie=curie,
            name=str(node.get("name") or curie),
            category=display_category(node.get("categories") or []),
            score=float(score) if isinstance(score, int | float) else None,
            bound_edge_ids=bound,
            support_graph_ids=[str(g) for g in first.get("support_graphs") or []],
        ))
    return answers


def _edge_support_graphs(edge: dict[str, Any]) -> list[str]:
    graphs: list[str] = []
    for attribute in edge.get("attributes") or []:
        if isinstance(attribute, dict) and attribute.get("attribute_type_id") == SUPPORT_GRAPHS:
            value = attribute.get("value")
            if isinstance(value, list):
                graphs.extend(str(g) for g in value)
    return graphs


def support_edges(body: dict[str, Any], answer: AraxAnswer) -> list[str]:
    # breadth-first over the nesting: edges first, then the support
    # graphs they (and the analysis) point to, then those graphs' edges.
    # each edge and each graph is visited once, so a cycle of support
    # graphs cannot loop.
    _, edges = _kg(body)
    auxiliary = _message(body).get("auxiliary_graphs") or {}
    ordered: list[str] = []
    seen_edges: set[str] = set()
    seen_graphs: set[str] = set()
    edge_queue: deque[str] = deque(answer.bound_edge_ids)
    graph_queue: deque[str] = deque(answer.support_graph_ids)
    while edge_queue or graph_queue:
        if edge_queue:
            edge_id = edge_queue.popleft()
            if edge_id in seen_edges or not isinstance(edges.get(edge_id), dict):
                continue
            seen_edges.add(edge_id)
            ordered.append(edge_id)
            graph_queue.extend(_edge_support_graphs(edges[edge_id]))
        else:
            graph_id = graph_queue.popleft()
            if graph_id in seen_graphs:
                continue
            seen_graphs.add(graph_id)
            graph = auxiliary.get(graph_id) or {}
            edge_queue.extend(str(e) for e in graph.get("edges") or [])
    return ordered


def evidence_edges(body: dict[str, Any], answer: AraxAnswer) -> list[str]:
    # a bound edge that carries support graphs is ARAX's own conclusion
    # ("X treats MS", inferred): drawing it as a reasoning step would be
    # circular, so it is left out and its support is what counts. a bound
    # edge without support graphs is a stored fact ARAX looked up (a query
    # without knowledge_type "inferred"), and that fact IS the evidence.
    _, edges = _kg(body)
    conclusions = {
        edge_id for edge_id in answer.bound_edge_ids
        if isinstance(edges.get(edge_id), dict) and _edge_support_graphs(edges[edge_id])
    }
    return [edge_id for edge_id in support_edges(body, answer) if edge_id not in conclusions]


def answer_paths(
    body: dict[str, Any],
    answer: AraxAnswer,
    target: str,
    *,
    max_hops: int,
    limit: int,
) -> list[list[str]]:
    # simple paths over the evidence, direction ignored: "drug affects
    # gene" and "gene associated with disease" chain into one line of
    # reasoning whichever way each fact is stored.
    _, edges = _kg(body)
    neighbours: dict[str, set[str]] = {}
    for edge_id in evidence_edges(body, answer):
        subject, obj = str(edges[edge_id]["subject"]), str(edges[edge_id]["object"])
        neighbours.setdefault(subject, set()).add(obj)
        neighbours.setdefault(obj, set()).add(subject)
    found: list[list[str]] = []
    queue: deque[list[str]] = deque([[answer.curie]])
    explored = 0
    while queue and explored < MAX_EXPLORED_PATHS:
        path = queue.popleft()
        explored += 1
        if path[-1] == target and len(path) > 1:
            found.append(path)
            continue
        if len(path) - 1 >= max_hops:
            continue
        for neighbour in sorted(neighbours.get(path[-1], ())):
            if neighbour not in path:
                queue.append([*path, neighbour])
    found.sort(key=lambda p: (len(p), p))
    return found[:limit]


def _edge_rank(edge_id: str, edge: dict[str, Any]) -> tuple[int, int, str]:
    attributes = {
        a.get("attribute_type_id"): a.get("value")
        for a in edge.get("attributes") or [] if isinstance(a, dict)
    }
    level = str(attributes.get("biolink:knowledge_level") or NOT_PROVIDED)
    agent = str(attributes.get("biolink:agent_type") or NOT_PROVIDED)
    return (
        KNOWLEDGE_LEVEL_RANK.get(level, KNOWLEDGE_LEVEL_RANK[NOT_PROVIDED]),
        AGENT_TYPE_RANK.get(agent, AGENT_TYPE_RANK[NOT_PROVIDED]),
        edge_id,
    )


def _edges_between(
    body: dict[str, Any], candidate_ids: list[str], left: str, right: str,
) -> list[str]:
    # every candidate edge joining the two nodes, strongest evidence first.
    _, edges = _kg(body)
    joining = [
        edge_id for edge_id in candidate_ids
        if {str(edges[edge_id]["subject"]), str(edges[edge_id]["object"])} == {left, right}
    ]
    return sorted(joining, key=lambda edge_id: _edge_rank(edge_id, edges[edge_id]))


def _primary_source(edge: dict[str, Any]) -> str | None:
    for source in edge.get("sources") or []:
        if isinstance(source, dict) and source.get("resource_role") == "primary_knowledge_source":
            return str(source.get("resource_id"))
    return None


def source_label(infores: str | None) -> str:
    if not infores:
        return "source not recorded"
    return SOURCE_NAMES.get(infores, infores.removeprefix("infores:"))


def _upstream_sources(edge: dict[str, Any]) -> list[str]:
    # where the primary source got the fact from (AACT from
    # ClinicalTrials.gov, the drug-approvals graph from DailyMed and
    # FAERS), readable and in order, without the aggregators.
    names: list[str] = []
    for source in edge.get("sources") or []:
        if isinstance(source, dict) and source.get("resource_role") == "supporting_data_source":
            name = source_label(str(source.get("resource_id")))
            if name not in names:
                names.append(name)
    return names


def _record_url(edge: dict[str, Any]) -> str | None:
    for source in edge.get("sources") or []:
        if isinstance(source, dict) and source.get("resource_role") == "primary_knowledge_source":
            urls = source.get("source_record_urls") or []
            if urls:
                return str(urls[0])
    return None


def _phase_text(value: Any) -> str | None:
    # "clinical_trial_phase_4" -> "4", "clinical_trial_phase_1_to_2" ->
    # "1/2", "pre_clinical_research_phase" -> "pre-clinical".
    text = str(value or "")
    if not text or text == "not_provided":
        return None
    if text.startswith("pre_clinical"):
        return "pre-clinical"
    if text.startswith("clinical_trial_phase_"):
        return text.removeprefix("clinical_trial_phase_").replace("_to_", "/")
    return text.replace("_", " ")


def _phase_order(phase: str | None) -> float:
    if not phase or phase == "pre-clinical":
        return 0.0
    try:
        return float(phase.split("/")[-1])
    except ValueError:
        return 0.0


def _trials(value: Any) -> list[dict[str, Any]]:
    # biolink:has_supporting_studies holds one record per registered
    # trial; the most advanced (then largest) come first.
    if not isinstance(value, dict):
        return []
    trials = []
    for key, study in value.items():
        record = study if isinstance(study, dict) else {}
        trial_id = str(record.get("id") or key).removeprefix("CLINICALTRIALS:")
        enrollment = record.get("clinical_trial_enrollment")
        trials.append({
            "id": trial_id,
            "url": f"https://clinicaltrials.gov/study/{trial_id}",
            "title": record.get("name"),
            "phase": _phase_text(record.get("clinical_trial_phase")),
            "status": str(record.get("clinical_trial_overall_status") or "").lower().replace("_", " ") or None,
            "start": record.get("clinical_trial_start_date"),
            "enrollment": enrollment if isinstance(enrollment, int) else None,
        })
    return sorted(trials, key=lambda t: (-_phase_order(t["phase"]), -(t["enrollment"] or 0), t["id"]))


def _ngd(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 3) if number == number and number != float("inf") else None


def fact_evidence(edge: dict[str, Any]) -> dict[str, Any]:
    # what a fact rests on, in the terms a reader checks: an approval,
    # registered trials, a drug label, papers, or how often two things
    # are mentioned together in PubMed. absent kinds are empty or None.
    attributes = {
        a.get("attribute_type_id"): a.get("value")
        for a in edge.get("attributes") or [] if isinstance(a, dict)
    }
    publications = attributes.get("biolink:publications")
    publication_list = [str(p) for p in publications] if isinstance(publications, list) else []
    pmids = [p for p in publication_list if p.upper().startswith("PMID:")]
    labels = [
        {"id": p, "url": f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={p.split(':', 1)[1]}"}
        for p in publication_list if p.lower().startswith("dailymed:")
    ]
    approvals = attributes.get("biolink:FDA_regulatory_approvals")
    trials = _trials(attributes.get("biolink:has_supporting_studies"))
    approval = attributes.get("biolink:clinical_approval_status")
    return {
        "approval": APPROVAL_WORDS.get(str(approval), str(approval).replace("_", " "))
        if approval and approval != "not_provided" else None,
        "max_phase": _phase_text(attributes.get("biolink:max_research_phase")),
        "fda_approvals": [str(a) for a in approvals] if isinstance(approvals, list) else [],
        "labels": labels,
        "trials": trials[:MAX_TRIALS_PER_FACT],
        "n_trials": len(trials),
        "pmids": pmids[:MAX_PMIDS_PER_FACT],
        "n_pmids": len(pmids),
        "ngd": _ngd(attributes.get("EDAM-DATA:2526")),
        "via": _upstream_sources(edge),
        "record_url": _record_url(edge),
    }


def _node_view(nodes: dict[str, Any], curie: str, role: str) -> dict[str, Any]:
    node = nodes.get(curie) or {}
    return {
        "id": curie,
        "label": str(node.get("name") or curie),
        "category": display_category(node.get("categories") or []),
        "role": role,
    }


def _edge_view(edge_id: str, edge: dict[str, Any]) -> dict[str, Any]:
    attributes = {
        a.get("attribute_type_id"): a.get("value")
        for a in edge.get("attributes") or [] if isinstance(a, dict)
    }
    publications = attributes.get("biolink:publications")
    publication_list = [str(p) for p in publications] if isinstance(publications, list) else []
    primary = _primary_source(edge)
    return {
        "id": edge_id,
        "source": str(edge["subject"]),
        "target": str(edge["object"]),
        "predicate": str(edge.get("predicate") or ""),
        "primary_source": primary,
        "source_label": source_label(primary),
        "knowledge_level": attributes.get("biolink:knowledge_level"),
        "agent_type": attributes.get("biolink:agent_type"),
        "publications": publication_list[:MAX_PUBLICATIONS_PER_EDGE],
        "evidence": fact_evidence(edge),
        "answers": [],
    }


def reasoning_graph(
    body: dict[str, Any],
    answers: list[AraxAnswer],
    target: str,
    *,
    paths_per_answer: int,
    target_label: str | None = None,
    target_category: str | None = None,
) -> dict[str, Any]:
    # only what lies on a chosen path is drawn: a gene-mediated
    # prediction can carry 60+ evidence edges, and the reader needs the
    # few chains that connect the answer to the question, not all of them.
    nodes, edges = _kg(body)
    paths = {
        answer.curie: answer_paths(
            body, answer, target, max_hops=MAX_PATH_HOPS, limit=paths_per_answer,
        )
        for answer in answers
    }
    answer_ids = [answer.curie for answer in answers]
    intermediates = sorted({
        curie for answer_paths_ in paths.values() for path in answer_paths_ for curie in path
    } - {target, *answer_ids})

    edge_views: dict[str, dict[str, Any]] = {}
    for answer in answers:
        evidence = evidence_edges(body, answer)
        for path in paths[answer.curie]:
            for left, right in pairwise(path):
                for edge_id in _edges_between(body, evidence, left, right):
                    view = edge_views.setdefault(edge_id, _edge_view(edge_id, edges[edge_id]))
                    if answer.curie not in view["answers"]:
                        view["answers"].append(answer.curie)

    # when ARAX found nothing its graph has no node for the question's
    # entity, so the resolved label and category stand in.
    query_node = _node_view(nodes, target, "query")
    if target not in nodes:
        query_node = {
            **query_node,
            "label": target_label or query_node["label"],
            "category": target_category or query_node["category"],
        }
    return {
        "query": target,
        "nodes": [
            query_node,
            *(_node_view(nodes, curie, "answer") for curie in answer_ids),
            *(_node_view(nodes, curie, "intermediate") for curie in intermediates),
        ],
        "edges": list(edge_views.values()),
        "paths": paths,
    }


def _short(predicate: str) -> str:
    return predicate.removeprefix("biolink:")


def _path_text(body: dict[str, Any], answer: AraxAnswer, path: list[str]) -> str:
    # one readable line per path, each step through its strongest fact:
    # "Baclofen -applied_to_treat-> relapsing-remitting multiple
    # sclerosis -subclass_of-> multiple sclerosis". an arrow pointing
    # back ("<-affects-") keeps the stored direction honest.
    nodes, edges = _kg(body)
    evidence = evidence_edges(body, answer)

    def name(curie: str) -> str:
        return str((nodes.get(curie) or {}).get("name") or curie)

    text = name(path[0])
    for left, right in pairwise(path):
        joining = _edges_between(body, evidence, left, right)
        if not joining:
            return text
        edge = edges[joining[0]]
        predicate = _short(str(edge.get("predicate") or ""))
        arrow = f" -{predicate}-> " if str(edge["subject"]) == left else f" <-{predicate}- "
        text += arrow + name(right)
    return text


def render_answers_table(
    body: dict[str, Any],
    answers: list[AraxAnswer],
    target: str,
    *,
    paths_per_answer: int,
) -> str:
    # the Stage 11 view of an ARAX reply: one line per answer in ARAX's
    # order, with its score, how much evidence backs it, and its
    # shortest reasoning path(s), each step through its strongest fact.
    _, edges = _kg(body)
    header = (
        f"ARAX ranked {len(answers)} answers. One line per answer, in ARAX's order: "
        "rank | name (CURIE) | score=<ARAX score, higher is better> | "
        "evidence=<number of supporting facts> from <primary sources> | "
        "path: <shortest reasoning path from the answer to the question's entity, "
        "each step through its strongest fact; <-pred- marks a fact stored in the "
        "other direction>."
    )
    lines = [header, ""]
    for answer in answers:
        evidence = evidence_edges(body, answer)
        sources = sorted({
            source for edge_id in evidence
            if (source := _primary_source(edges[edge_id])) is not None
        })
        score = f"{answer.score:.3f}" if answer.score is not None else "n/a"
        line = (
            f"{answer.rank} | {answer.name} ({answer.curie}) | score={score} | "
            f"evidence={len(evidence)} from {', '.join(sources[:4]) or 'none'}"
        )
        for path in answer_paths(
            body, answer, target, max_hops=MAX_PATH_HOPS, limit=paths_per_answer,
        ):
            line += f" | path: {_path_text(body, answer, path)}"
        lines.append(line)
    return "\n".join(lines)


def number_facts(graph: dict[str, Any]) -> dict[str, Any]:
    # the explanation cites facts as [F1], [F2] rather than ARAX's
    # 36-character edge ids, and the UI maps each F-number back to the
    # edge it highlights. ids follow the graph's own edge order, which
    # is already deterministic. path_facts restates every path as its
    # steps, each step the facts joining its two nodes, strongest first.
    edges = [{**edge, "fact_id": f"F{i}"} for i, edge in enumerate(graph["edges"], start=1)]
    by_pair: dict[frozenset[str], list[str]] = {}
    for edge in edges:
        by_pair.setdefault(frozenset((edge["source"], edge["target"])), []).append(edge["fact_id"])
    path_facts = {
        answer: [
            [by_pair.get(frozenset(step), []) for step in pairwise(path)]
            for path in paths
        ]
        for answer, paths in graph["paths"].items()
    }
    return {**graph, "edges": edges, "path_facts": path_facts}


def evidence_text(edge: dict[str, Any]) -> str:
    # one fact's evidence in plain words, for the explainer's fact list:
    # the source, the evidence level, then whatever the fact rests on.
    level = str(edge.get("knowledge_level") or "not_provided")
    agent = str(edge.get("agent_type") or "not_provided")
    source = edge.get("source_label") or source_label(edge.get("primary_source"))
    parts = [
        f"source: {source}",
        f"{KNOWLEDGE_LEVEL_WORDS.get(level, level.replace('_', ' '))}, "
        f"{AGENT_TYPE_WORDS.get(agent, agent.replace('_', ' '))}",
    ]
    evidence = edge.get("evidence") or {}
    if evidence.get("via"):
        parts.append("from " + ", ".join(evidence["via"]))
    if evidence.get("approval"):
        parts.append(str(evidence["approval"]))
    if evidence.get("fda_approvals"):
        parts.append("FDA application " + ", ".join(evidence["fda_approvals"][:3]))
    if evidence.get("labels"):
        n = len(evidence["labels"])
        parts.append(f"{n} FDA drug label{'s' if n != 1 else ''} on DailyMed")
    if evidence.get("n_trials"):
        n = evidence["n_trials"]
        text = f"{n} registered clinical trial{'s' if n != 1 else ''}"
        if evidence.get("max_phase"):
            text += f", most advanced phase {evidence['max_phase']}"
        top = evidence["trials"][0]
        described = ", ".join(
            bit for bit in (
                f"phase {top['phase']}" if top.get("phase") else None,
                top.get("status"),
                f"{top['enrollment']} participants" if top.get("enrollment") else None,
            ) if bit
        )
        text += f" (e.g. {top['id']}" + (f", {described}" if described else "") + ")"
        parts.append(text)
    elif evidence.get("max_phase"):
        parts.append(f"most advanced research phase {evidence['max_phase']}")
    if evidence.get("ngd") is not None:
        parts.append(
            f"normalized co-mention distance in PubMed {evidence['ngd']:.2f} "
            "(lower means mentioned together more often; above 1 is weak)"
        )
    if evidence.get("n_pmids"):
        shown = ", ".join(evidence["pmids"][:3])
        parts.append(f"{evidence['n_pmids']} PubMed article{'s' if evidence['n_pmids'] != 1 else ''} ({shown})")
    return " | ".join(parts)


def render_facts_block(graph: dict[str, Any]) -> str:
    # what Stage 15 cites from: every fact on a drawn path with its
    # evidence in plain words, then each answer's paths written as steps
    # of fact ids. a step with several facts means independent sources
    # state the same link.
    labels = {node["id"]: node["label"] for node in graph["nodes"]}
    lines = ["Reasoning facts (cite them as [F<n>]):"]
    for edge in graph["edges"]:
        lines.append(
            f"{edge['fact_id']} | {labels.get(edge['source'], edge['source'])} "
            f"--{_short(edge['predicate']).replace('_', ' ')}--> {labels.get(edge['target'], edge['target'])} "
            f"| {evidence_text(edge)}"
        )
    lines.extend(["", "Reasoning paths per answer, in ARAX rank order (each step lists the facts that support it):"])
    for answer, paths in graph["path_facts"].items():
        lines.append(f"{labels.get(answer, answer)} ({answer}):")
        if not paths:
            lines.append("  no path to the question's entity within the evidence ARAX returned")
        for number, path in enumerate(paths, start=1):
            nodes = graph["paths"][answer][number - 1]
            chain = " -> ".join(labels.get(node, node) for node in nodes)
            steps = " -> ".join("[" + ", ".join(step) + "]" for step in path)
            lines.append(f"  path {number}: {chain}  facts: {steps}")
    return "\n".join(lines)
