# translator_assets.py — converts the NCATS Translator "Tests"
# repository (github.com/NCATSTranslator/Tests) into PloverAI gold
# question records. the repository holds ~655 expert-labelled assets,
# each ONE (input, predicate, output) triple with a label such as
# TopAnswer / Acceptable / NeverShow. a QUESTION here is the group of
# assets sharing (input_id, predicate, direction, aspect): "what
# chemicals may decrease the activity or abundance of CYP2D6?" with
# the group's outputs as its labelled answers.
#
# why this set: it is the only expert-labelled answer set for the
# Translator ecosystem ARAX and the Tier 0 graph belong to, and its
# NeverShow labels
# let the benchmark measure harm (answers experts said must never be
# shown), not only hits.
#
# caveats recorded in every record and in manifest.json:
#   - the assets were written for Translator's multi-hop "inferred"
#     mode, so some labelled answers are not reachable in one hop.
#     every labelled answer carries its one-hop reachability, measured
#     at conversion time, so scores can be read against that ceiling.
#   - labels exist only for outputs Translator once surfaced (open
#     world): an unlabelled pick is unknown, not wrong.
#   - the gold side is canonicalised through Node Normalization HERE,
#     once, and frozen into the record, so the scorer can stay offline.
#     without this step a direct id comparison undercounts affects
#     answers about five-fold (9 vs 43 of 118 positives reachable).
#
# usage (from the project root, with a local clone of the Tests repo):
#   pipeline/.venv/bin/python -m pipeline.code.translator_assets \
#       --tests-dir /path/to/Tests

from __future__ import annotations

# argparse: stdlib. one required flag and two optional ones.
import argparse

# json: stdlib. assets, paraphrases and output records are JSON.
import json

# subprocess: stdlib. reads the Tests clone's commit SHA so every record
# names the exact asset version it was built from.
import subprocess

# collections.Counter: stdlib. label tallies and the most-frequent
# input name per question.
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# our modules: config (paths, endpoints), the two service clients
# whose requests are logged like every other pipeline call, and the
# run-stamp / logger helpers the runner uses.
from .config import PIPELINE_ROOT, load_config
from .logging_setup import setup_logger, utc_stamp
from .nodenorm_client import NodeNormClient, NodeNormReply
from .retriever_client import RetrieverClient, RetrieverError

# where the converted records go, next to the hand-curated q*.json set.
OUTPUT_DIR = PIPELINE_ROOT / "benchmark" / "golden_questions" / "translator"

# hand-written paraphrases (one natural rewording per question, keyed
# by question_key_text). kept in its own file so the machine-generated
# records and the human/LLM-written text are never mixed up.
PARAPHRASES_FILE = OUTPUT_DIR / "paraphrases.json"

POSITIVE_LABELS = frozenset({"TopAnswer", "Acceptable"})

# input ids whose asset name would put a data-entry error into the
# "clean" template phrasing. the template is meant to be the easy,
# canonical wording, so these are corrected here, openly:
#   CHEBI:5558     misspelled "Guafacine" in the assets;
#   MONDO:0017314  named "Ehlers-Danlos Syndrome" in the assets, but the
#                  id (and so every expert label) is the vascular type.
NAME_OVERRIDES: dict[str, str] = {
    "CHEBI:5558": "Guanfacine",
    "MONDO:0017314": "vascular Ehlers-Danlos syndrome",
}

# input ids dropped from the set, with the reason written into
# manifest.json. an exclusion is for a broken gold side only, never
# for how a model did on the question.
EXCLUDED_INPUTS: dict[str, str] = {
    "RXCUI:151392": (
        "Augmentin normalises to UMLS:C5965653 'Kesium' (a veterinary "
        "brand), so the gold entity cannot be resolved from any wording "
        "of the question"
    ),
}

# how the Biolink aspect qualifier reads in English.
ASPECT_TEXT: dict[str, str] = {
    "activity_or_abundance": "activity or abundance",
    "activity": "activity",
    "abundance": "abundance",
}

# the question-type order used to number records (tr01 ...).
QUESTION_TYPE_ORDER = ("treats", "affects_decreased", "affects_increased")

GENE_CATEGORY = "biolink:Gene"
CHEMICAL_CATEGORY = "biolink:ChemicalEntity"


# ------------------------------------------------------ pure functions


def _qualifier_value(asset: dict[str, Any], parameter: str) -> str | None:
    for qualifier in asset.get("qualifiers") or []:
        if isinstance(qualifier, dict) and qualifier.get("parameter") == parameter:
            value = qualifier.get("value")
            return value if isinstance(value, str) and value else None
    return None


def asset_direction(asset: dict[str, Any]) -> str | None:
    return _qualifier_value(asset, "biolink_object_direction_qualifier")


def asset_aspect(asset: dict[str, Any]) -> str | None:
    return _qualifier_value(asset, "biolink_object_aspect_qualifier")


def question_key(asset: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
    return (
        str(asset["input_id"]),
        str(asset["predicate_id"]),
        asset_direction(asset),
        asset_aspect(asset),
    )


def question_key_text(key: tuple[str, str, str | None, str | None]) -> str:
    # the stable string form used to key paraphrases.json, so a
    # paraphrase stays attached to its question if numbering changes.
    input_id, predicate, direction, aspect = key
    return f"{input_id}|{predicate}|{direction or '-'}|{aspect or '-'}"


def clean_input_name(name: str) -> str:
    text = name.strip()
    suffix = " (human)"
    if text.lower().endswith(suffix):
        text = text[: -len(suffix)].rstrip()
    return text


def pick_question_name(input_id: str, names: list[str]) -> str:
    if input_id in NAME_OVERRIDES:
        return NAME_OVERRIDES[input_id]
    counts = Counter(clean_input_name(name) for name in names)
    return min(counts, key=lambda name: (-counts[name], len(name), name))


def question_type(predicate: str, direction: str | None) -> str:
    if predicate == "biolink:treats":
        return "treats"
    return f"affects_{direction}"


def answer_category_for(predicate: str, *, pinned_is_gene: bool) -> str:
    # treats questions ask for drugs; affects questions ask for the
    # other side of a chemical-gene edge.
    if predicate == "biolink:treats" or pinned_is_gene:
        return CHEMICAL_CATEGORY
    return GENE_CATEGORY


def template_question(
    predicate: str,
    *,
    pinned_is_gene: bool,
    name: str,
    direction: str | None,
    aspect: str | None,
) -> str:
    if predicate == "biolink:treats":
        return f"What drugs may treat {name}?"
    verb = "decrease" if direction == "decreased" else "increase"
    aspect_text = ASPECT_TEXT.get(aspect or "", "activity or abundance")
    if pinned_is_gene:
        return f"What chemicals may {verb} the {aspect_text} of {name}?"
    return f"Which genes may {name} {verb} the {aspect_text} of?"


def qualifier_set(direction: str, aspect: str | None) -> list[dict[str, str]]:
    return [
        {"qualifier_type_id": "biolink:qualified_predicate", "qualifier_value": "biolink:causes"},
        {"qualifier_type_id": "biolink:object_aspect_qualifier",
         "qualifier_value": aspect or "activity_or_abundance"},
        {"qualifier_type_id": "biolink:object_direction_qualifier", "qualifier_value": direction},
    ]


def gold_query_graph(
    *,
    pinned_curie: str,
    pinned_category: str,
    answer_category: str,
    predicate: str,
    pinned_is_gene: bool,
    direction: str | None,
    aspect: str | None,
) -> dict[str, Any]:
    # oriented the way the graph stores the fact: the drug treats the
    # disease, the chemical affects the gene. the answer node (n1) is
    # the subject unless a chemical is pinned on an affects question.
    answer_is_subject = predicate == "biolink:treats" or pinned_is_gene
    edge: dict[str, Any] = {
        "subject": "n1" if answer_is_subject else "n0",
        "object": "n0" if answer_is_subject else "n1",
        "predicates": [predicate],
    }
    if direction:
        edge["qualifier_constraints"] = [{"qualifier_set": qualifier_set(direction, aspect)}]
    return {
        "nodes": {
            "n0": {"ids": [pinned_curie], "categories": [pinned_category]},
            "n1": {"categories": [answer_category]},
        },
        "edges": {"e0": edge},
    }


def is_positive(label: str) -> bool:
    return label in POSITIVE_LABELS


def is_usable(labelled_answers: list[dict[str, Any]], answer_category: str) -> bool:
    # an output of another category can never be picked by a query for
    # answer_category, so a question whose labels all sit elsewhere
    # cannot score anything. "unknown category" is given the benefit.
    return any(
        answer.get("output_category") in (answer_category, None)
        for answer in labelled_answers
    )


# ------------------------------------------------------- network steps


def _answer_id_sets(labelled: list[dict[str, Any]]) -> list[frozenset[str]]:
    return [
        frozenset({answer["asset_curie"], answer["canonical_curie"], *answer["equivalent_curies"]})
        for answer in labelled
    ]


def _bound_answer_ids(body: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for result in body.get("message", {}).get("results") or []:
        for binding in (result.get("node_bindings") or {}).get("n1") or []:
            if isinstance(binding, dict) and isinstance(binding.get("id"), str):
                ids.add(binding["id"])
    return ids


def _reachable(
    retriever: RetrieverClient, graph: dict[str, Any], id_sets: list[frozenset[str]],
) -> tuple[list[bool], int]:
    try:
        reply = retriever.query({"message": {"query_graph": graph}})
    except RetrieverError:
        return [False] * len(id_sets), -1
    bound = _bound_answer_ids(reply.body)
    n_results = len(reply.body.get("message", {}).get("results") or [])
    return [bool(ids & bound) for ids in id_sets], n_results


def _pinned_pair_graph(
    gold: dict[str, Any], answer_curies: list[str], predicate: str, keep_qualifiers: bool,
) -> dict[str, Any]:
    # the same orientation as the gold query, but the answer node is
    # pinned to the labelled answers so the response stays small even
    # for a broad predicate like related_to on a common disease.
    edge = dict(gold["edges"]["e0"])
    edge["predicates"] = [predicate]
    if not keep_qualifiers:
        edge.pop("qualifier_constraints", None)
    return {
        "nodes": {
            "n0": dict(gold["nodes"]["n0"]),
            "n1": {"ids": answer_curies},
        },
        "edges": {"e0": edge},
    }


def _labelled_answers(assets: list[dict[str, Any]], nodenorm: NodeNormReply) -> list[dict[str, Any]]:
    answers = []
    for asset in sorted(assets, key=lambda a: (not is_positive(a["expected_output"]), a["output_id"])):
        curie = str(asset["output_id"])
        answers.append({
            "asset_id": asset["id"],
            "asset_curie": curie,
            "label": asset.get("output_name"),
            "expected_output": asset["expected_output"],
            "output_category": asset.get("output_category"),
            "canonical_curie": nodenorm.canonical.get(curie) or curie,
            "equivalent_curies": sorted(nodenorm.equivalent_identifiers.get(curie) or []),
        })
    return answers


def _git_commit(tests_dir: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(tests_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return completed.stdout.strip()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ploverai-translator-assets",
        description="convert NCATS Translator Tests assets into PloverAI gold records.",
    )
    p.add_argument("--tests-dir", required=True, type=Path,
                   help="local clone of github.com/NCATSTranslator/Tests")
    p.add_argument("--dry-run", action="store_true",
                   help="group and print the questions; no network calls, no files written.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    assets = [
        json.loads(path.read_text())
        for path in sorted((args.tests_dir / "test_assets").glob("Asset_*.json"))
    ]
    groups: dict[tuple[str, str, str | None, str | None], list[dict[str, Any]]] = {}
    for asset in assets:
        if asset.get("input_id") and asset.get("output_id") and asset.get("predicate_id"):
            groups.setdefault(question_key(asset), []).append(asset)

    if args.dry_run:
        for key, members in sorted(groups.items(), key=lambda kv: question_key_text(kv[0])):
            names = [str(a.get("input_name") or "") for a in members]
            print(f"{question_key_text(key)}  {pick_question_name(key[0], names)!r}  n={len(members)}")
        print(f"{len(assets)} assets -> {len(groups)} questions")
        return 0

    cfg = load_config()
    stamp = utc_stamp()
    logger, log_path = setup_logger(cfg.paths.logs, f"translator-assets_{stamp}")
    nodenorm = NodeNormClient(cfg, logger)
    retriever = RetrieverClient(cfg, logger)
    paraphrases: dict[str, str] = (
        json.loads(PARAPHRASES_FILE.read_text()) if PARAPHRASES_FILE.exists() else {}
    )
    commit = _git_commit(args.tests_dir)
    converted_on = datetime.now(tz=UTC).date().isoformat()

    drafts: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for key, members in groups.items():
        input_id, predicate, direction, aspect = key
        names = [str(a.get("input_name") or "") for a in members]
        name = pick_question_name(input_id, names)
        if input_id in EXCLUDED_INPUTS:
            excluded.append({"key": question_key_text(key), "name": name,
                             "reason": EXCLUDED_INPUTS[input_id]})
            continue
        output_ids = sorted({str(a["output_id"]) for a in members})
        nn = nodenorm.normalize([input_id, *output_ids])
        pinned_curie = nn.canonical.get(input_id)
        if not pinned_curie:
            excluded.append({"key": question_key_text(key), "name": name,
                             "reason": "input id does not normalise"})
            continue
        pinned_categories = nn.categories.get(input_id) or []
        pinned_is_gene = GENE_CATEGORY in pinned_categories
        answer_category = answer_category_for(predicate, pinned_is_gene=pinned_is_gene)
        labelled = _labelled_answers(members, nn)
        if not is_usable(labelled, answer_category):
            excluded.append({"key": question_key_text(key), "name": name,
                             "reason": f"no labelled answer in {answer_category}"})
            continue
        gold = gold_query_graph(
            pinned_curie=pinned_curie, pinned_category=pinned_categories[0],
            answer_category=answer_category, predicate=predicate,
            pinned_is_gene=pinned_is_gene, direction=direction, aspect=aspect,
        )
        id_sets = _answer_id_sets(labelled)
        answer_curies = sorted({answer["canonical_curie"] for answer in labelled})
        gold_hits, gold_n = _reachable(retriever, gold, id_sets)
        predicate_hits, _ = _reachable(
            retriever, _pinned_pair_graph(gold, answer_curies, predicate, False), id_sets,
        )
        any_hits, _ = _reachable(
            retriever, _pinned_pair_graph(gold, answer_curies, "biolink:related_to", False), id_sets,
        )
        for answer, hit_gold, hit_predicate, hit_any in zip(
            labelled, gold_hits, predicate_hits, any_hits, strict=True,
        ):
            answer["reachable_one_hop"] = {
                "gold_query": hit_gold,
                "predicate_without_qualifiers": hit_predicate,
                "any_predicate": hit_any,
            }
        drafts.append({
            "question_type": question_type(predicate, direction),
            "source": {
                "repo": "https://github.com/NCATSTranslator/Tests",
                "commit": commit,
                "asset_ids": sorted(a["id"] for a in members),
                "question_key": question_key_text(key),
                "converted_on": converted_on,
            },
            "split": "test",
            "phrasings": {
                "template": template_question(
                    predicate, pinned_is_gene=pinned_is_gene, name=name,
                    direction=direction, aspect=aspect,
                ),
                "paraphrase": paraphrases.get(question_key_text(key)),
            },
            "pinned_entity": {
                "curie": pinned_curie,
                "label": nn.labels.get(input_id),
                "category": pinned_categories[0],
                "asset_curie": input_id,
                "asset_name": name,
            },
            "answer_category": answer_category,
            "predicate": predicate,
            "qualifiers": qualifier_set(direction, aspect) if direction else [],
            "gold_query_graph": gold,
            "gold_query_results": gold_n,
            "label_counts": dict(Counter(a["expected_output"] for a in labelled)),
            "labelled_answers": labelled,
            "kg": {"retriever_url": cfg.endpoints.retriever, "tiers": list(cfg.retriever.tiers), "probed_on": converted_on},
        })

    drafts.sort(key=lambda d: (
        QUESTION_TYPE_ORDER.index(d["question_type"]),
        str(d["pinned_entity"]["asset_name"]).lower(),
    ))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # every run rewrites the whole set, and numbering shifts when a
    # question is added or excluded, so the old records go first: a
    # stale tr82.json must never survive a run that writes only 81.
    for stale in OUTPUT_DIR.glob("tr*.json"):
        stale.unlink()
    width = max(2, len(str(len(drafts))))
    missing_paraphrases = []
    for number, draft in enumerate(drafts, start=1):
        question_id = f"tr{number:0{width}d}"
        record = {"question_id": question_id, **draft}
        (OUTPUT_DIR / f"{question_id}.json").write_text(json.dumps(record, indent=2) + "\n")
        if not draft["phrasings"]["paraphrase"]:
            missing_paraphrases.append(question_id)

    manifest = {
        "source_repo": "https://github.com/NCATSTranslator/Tests",
        "source_commit": commit,
        "converted_on": converted_on,
        "n_assets": len(assets),
        "n_questions_grouped": len(groups),
        "n_questions_written": len(drafts),
        "question_types": dict(Counter(d["question_type"] for d in drafts)),
        "label_counts": dict(sum(
            (Counter(d["label_counts"]) for d in drafts), Counter(),
        )),
        "excluded": excluded,
        "missing_paraphrases": missing_paraphrases,
    }
    (OUTPUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    logger.info(
        f"translator assets  written={len(drafts)}  excluded={len(excluded)}  "
        f"missing_paraphrases={len(missing_paraphrases)}  log={log_path}"
    )
    nodenorm.close()
    retriever.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
