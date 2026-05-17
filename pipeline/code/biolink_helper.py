# biolink_helper.py — Biolink Model-derived utilities.
#
# right now there's exactly one use: compute a "loose neighborhood" of
# Biolink categories for the NameRes biolink_type filter. the reason
# this exists is a class of pipeline failures where Stage 2 picks one
# Biolink category that's semantically correct in English but doesn't
# match how the entity is typed in KG2c — so NameRes filters out the
# right answer.
#
# concrete failure (q "Which genes participate in the cholesterol
# biosynthesis pathway?"):
#   - Stage 2 picks expected_category = biolink:Pathway
#   - the actual entity GO:0006695 ("cholesterol biosynthetic process")
#     is typed as biolink:BiologicalProcess in KG2c, NOT biolink:Pathway
#   - NameRes filtered to Pathway only → GO:0006695 is excluded →
#     BM25 falls back to matching just the token "process" and returns
#     5 unrelated "Processing..." pathways all at the same score 75.26
#   - Stage 4 correctly rejects the garbage, run fails with
#     status=no_candidate_match
#
# fix: when the LLM picks biolink:Pathway, also let NameRes match on
# biolink:BiologicalProcess (Pathway's immediate Biolink parent), so
# GO:0006695 stays in the candidate set. we do this via BMT (Biolink
# Model Toolkit) instead of hardcoding a per-category map — the Biolink
# Model itself owns the class hierarchy, and we just read from it.
#
# rule used:
#   loose_neighborhood(C) =
#       descendants(C)
#     ∪ (descendants(parent(C))  if parent(C) is not in GENERIC_PARENTS)
#
# the GENERIC_PARENTS stop-list catches Biolink umbrella classes that
# are too broad to be useful for narrowing entity resolution — e.g.
# biolink:BiologicalEntity is the parent of Gene, Disease, Protein,
# Pathway, ... so expanding via it would defeat the whole point of
# typing. the list is curated, not derived, but the per-category
# expansion IS derived from the live Biolink class hierarchy.

from __future__ import annotations

import logging

import bmt


# Biolink categories that are too generic to act as a "loose parent".
# when a category's immediate parent is in this set, we DON'T expand
# the filter to include all siblings — that would let NameRes return
# essentially anything biological.
#
# this is the only static list in this module. it captures Biolink's
# "umbrella" classes — the ones that have lots of unrelated children.
# all other expansions come from BMT's view of the class hierarchy.
GENERIC_PARENTS: frozenset[str] = frozenset({
    "biolink:Entity",
    "biolink:NamedThing",
    "biolink:ThingWithTaxon",
    "biolink:OntologyClass",
    "biolink:SubjectOfInvestigation",
    "biolink:PhysicalEssence",
    "biolink:Occurrent",
    "biolink:PhysicalEssenceOrOccurrent",
    "biolink:BiologicalEntity",         # parent of Gene, Disease, Protein, Pathway, ...
    "biolink:MolecularEntity",          # parent of ChemicalEntity, GenomicEntity, ...
    "biolink:ChemicalEntity",           # parent of Drug, Food, MolecularMixture, ...
    "biolink:ChemicalEntityOrGeneOrGeneProduct",
    "biolink:ChemicalEntityOrProteinOrPolypeptide",
    "biolink:GeneProductMixin",
    "biolink:OrganismalEntity",
    "biolink:GeneOrGeneProduct",
    "biolink:BiologicalProcessOrActivity",   # parent of BiologicalProcess + MolecularActivity
})


def make_toolkit() -> bmt.Toolkit:
    # one Toolkit per process. bmt loads ~10 MB of Biolink YAML on
    # construction so we want this called exactly once at boot. not
    # documented as thread-safe; we treat it as read-only after init.
    return bmt.Toolkit()


def compute_loose_neighborhood(
    category: str,
    toolkit: bmt.Toolkit,
) -> list[str]:
    # returns a sorted list of biolink:CategoryName strings that should
    # all be passed to NameRes as biolink_type filters when the user /
    # LLM picked `category`. always includes `category` itself. expands
    # to include the parent's descendants IF the parent is informative
    # (not in GENERIC_PARENTS).
    #
    # examples (BMT 1.4.6 + Biolink Model 4.x):
    #   biolink:Pathway → {biolink:Pathway, biolink:BiologicalProcess,
    #                      biolink:PhysiologicalProcess, biolink:Behavior,
    #                      biolink:PathologicalProcess}
    #   biolink:Disease → {biolink:Disease, biolink:PhenotypicFeature,
    #                      biolink:DiseaseOrPhenotypicFeature, ...}
    #   biolink:Gene    → {biolink:Gene}   (parent BiologicalEntity is
    #                                       in GENERIC_PARENTS → no expansion)
    #   biolink:Cell    → {biolink:Cell, biolink:AnatomicalEntity,
    #                      biolink:GrossAnatomicalStructure, ...}
    descendants = set(toolkit.get_descendants(category, formatted=True))
    descendants.add(category)

    parent = toolkit.get_parent(category, formatted=True)
    if parent and parent not in GENERIC_PARENTS:
        for sib in toolkit.get_descendants(parent, formatted=True):
            descendants.add(sib)
        descendants.add(parent)

    return sorted(descendants)


def build_neighborhood_map(
    available_categories: list[str],
    toolkit: bmt.Toolkit,
    logger: logging.Logger | None = None,
) -> dict[str, list[str]]:
    # precompute the loose neighborhood for every Biolink category the
    # KG actually carries. called once at api.py boot; the result is
    # passed down to NameRes-lookup callers. avoiding per-request BMT
    # work matters because Toolkit lookups, while cheap, are not free
    # and we're on a per-question latency budget.
    out: dict[str, list[str]] = {}
    for cat in available_categories:
        try:
            out[cat] = compute_loose_neighborhood(cat, toolkit)
        except Exception as e:
            # BMT can raise on categories it doesn't recognise (e.g. a
            # KG-specific extension class). fall back to the category
            # alone so the pipeline still runs; this is the same as the
            # old strict-filter behaviour.
            if logger is not None:
                logger.warning(
                    f"BMT loose-neighborhood lookup failed for {cat!r}: {e}. "
                    "Falling back to single-category filter."
                )
            out[cat] = [cat]
    return out


def loose_filter_for(
    category: str | None,
    neighborhood_map: dict[str, list[str]] | None,
) -> list[str] | None:
    # public convenience: given the LLM's picked category and the
    # precomputed map, return the list of biolink_types to send to
    # NameRes. returns None when the input is None or biolink:NamedThing
    # (means "no filter", matching the old behaviour). returns the
    # single category as a one-element list when the map is missing or
    # has no entry for this category — same fallback as the strict
    # filter, so we never make things worse.
    if not category or category == "biolink:NamedThing":
        return None
    if neighborhood_map is None:
        return [category]
    return neighborhood_map.get(category, [category])
