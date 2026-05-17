# _parse_pubtator_biocjson: extract per-PMID entity-CURIE sets from
# PubTator3's BioC-JSON response.
#
# the function consumes the JSON body returned by:
#   GET /publications/export/biocjson?pmids=<comma-separated-pmids>
# and returns dict[pmid_str -> set[curie_str]] where each set contains
# the canonical CURIEs of every entity PubTator annotated in that PMID.
#
# this is pure parsing — no network. the test fixtures are abridged
# real responses we captured during pipeline design (see git history
# for the verbatim curl that produced them). assertions are STRICT:
# wrong field name in the parser breaks one test; wrong handling of
# missing fields breaks another.
#
# spec:
#   1. for each document in body["PubTator3"][N], extract its pmid AND
#      its annotations (across all passages — abstract + title)
#   2. each annotation's identifier is at infons.identifier (e.g.
#      "MESH:D003924"). collect these into a set per PMID.
#   3. malformed / missing fields degrade gracefully — never raise.
#   4. PMIDs returned with NO annotations still appear as keys with
#      an empty set (so the caller can distinguish "no entities" from
#      "PubTator didn't index this PMID").
#   5. empty body → empty dict.

from ploverai.pubtator_client import _parse_pubtator_biocjson


def _minimal_biocjson():
    # representative fixture: 1 PMID, 2 passages (title + abstract),
    # 3 annotations spread across them. mirrors PubTator3's real shape.
    return {
        "PubTator3": [
            {
                "pmid": 33487311,
                "passages": [
                    {
                        "infons": {"type": "title"},
                        "annotations": [
                            {
                                "text": "vitamin D",
                                "infons": {
                                    "identifier": "MESH:D014807",
                                    "type": "Chemical",
                                    "biotype": "chemical",
                                },
                            }
                        ],
                    },
                    {
                        "infons": {"type": "abstract"},
                        "annotations": [
                            {
                                "text": "type 2 diabetes",
                                "infons": {
                                    "identifier": "MESH:D003924",
                                    "type": "Disease",
                                },
                            },
                            {
                                "text": "metformin",
                                "infons": {
                                    "identifier": "MESH:D008687",
                                    "type": "Chemical",
                                },
                            },
                        ],
                    },
                ],
            }
        ]
    }


# ---- happy path ----

def test_extracts_all_annotation_identifiers():
    # 3 annotations spread across 2 passages → expect 3 CURIEs collapsed
    # into one set for the single PMID.
    out = _parse_pubtator_biocjson(_minimal_biocjson())
    assert out == {
        "PMID:33487311": {"MESH:D014807", "MESH:D003924", "MESH:D008687"}
    }


def test_pmid_key_is_prefixed():
    # PubTator returns pmid as a bare integer; we must return CURIE-formatted
    # "PMID:<n>" so callers can compare directly with edge.supporting_publications
    # (which are CURIE-formatted in TRAPI).
    out = _parse_pubtator_biocjson(_minimal_biocjson())
    keys = list(out.keys())
    assert all(k.startswith("PMID:") for k in keys)


# ---- multi-document fixture ----

def test_multiple_documents_each_get_own_pmid_key():
    # PubTator3 can return multiple docs in one call (batched PMIDs).
    # we must produce one entry per pmid.
    body = {
        "PubTator3": [
            {
                "pmid": 100,
                "passages": [{
                    "annotations": [{
                        "infons": {"identifier": "MESH:A1", "type": "Chemical"}
                    }]
                }]
            },
            {
                "pmid": 200,
                "passages": [{
                    "annotations": [{
                        "infons": {"identifier": "MESH:B2", "type": "Disease"}
                    }]
                }]
            },
        ]
    }
    out = _parse_pubtator_biocjson(body)
    assert out == {"PMID:100": {"MESH:A1"}, "PMID:200": {"MESH:B2"}}


# ---- degraded inputs ----

def test_pmid_with_no_annotations_emits_empty_set():
    # PubTator indexed the PMID but found no entities. we must emit the
    # PMID with an empty set — NOT drop it — so callers can distinguish
    # "no entities found" from "PMID unknown to PubTator".
    body = {
        "PubTator3": [{
            "pmid": 33487311,
            "passages": [{"annotations": []}, {"annotations": []}],
        }]
    }
    out = _parse_pubtator_biocjson(body)
    assert out == {"PMID:33487311": set()}


def test_passage_with_no_annotations_key_does_not_raise():
    # some passages (especially auto-generated ones) lack the
    # "annotations" key entirely. parser must tolerate this.
    body = {
        "PubTator3": [{
            "pmid": 33487311,
            "passages": [
                {"infons": {"type": "title"}},  # no annotations key
                {"annotations": [{
                    "infons": {"identifier": "MESH:D003924", "type": "Disease"}
                }]},
            ],
        }]
    }
    out = _parse_pubtator_biocjson(body)
    assert out == {"PMID:33487311": {"MESH:D003924"}}


def test_annotation_missing_identifier_is_skipped():
    # an annotation with no identifier in infons (rare, but happens for
    # partial matches) must be skipped, NOT raise.
    body = {
        "PubTator3": [{
            "pmid": 33487311,
            "passages": [{
                "annotations": [
                    {"text": "metformin", "infons": {}},  # no identifier
                    {"text": "diabetes", "infons": {"identifier": "MESH:D003924"}},
                ]
            }]
        }]
    }
    out = _parse_pubtator_biocjson(body)
    assert out == {"PMID:33487311": {"MESH:D003924"}}


def test_empty_body_returns_empty_dict():
    # the most degenerate input — PubTator returned nothing useful.
    # must not raise; just empty result.
    assert _parse_pubtator_biocjson({}) == {}
    assert _parse_pubtator_biocjson({"PubTator3": []}) == {}


def test_non_string_identifier_is_skipped():
    # defensive: if PubTator ever returns a numeric or null identifier,
    # we drop it. asserting on this prevents a future schema drift
    # from sneaking a non-CURIE value into the output set.
    body = {
        "PubTator3": [{
            "pmid": 33487311,
            "passages": [{
                "annotations": [
                    {"infons": {"identifier": 12345}},  # int, not string
                    {"infons": {"identifier": None}},
                    {"infons": {"identifier": "MESH:OK"}},
                ]
            }]
        }]
    }
    out = _parse_pubtator_biocjson(body)
    assert out == {"PMID:33487311": {"MESH:OK"}}
