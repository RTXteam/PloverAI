# _parse_stage0_output: tolerant parser for Stage 2's 4-field JSON.
#
# Stage 2's LLM-side contract is:
#   {"entity": "...", "expected_category": "biolink:...",
#    "answer_category": "biolink:...", "granularity_preference": "general"|"specific"}
#
# the parser is intentionally tolerant — different LLMs sometimes emit
# slightly different shapes (legacy "name" instead of "entity", legacy
# "category" instead of "expected_category", missing fields, plain
# strings from older models). this file pins the exact tolerance
# behaviour. an over-eager "fix" that drops the legacy aliases would
# silently break runs against older snapshot models.
#
# behaviour spec for the four output fields:
#   - mention:           required-ish. empty string if absent.
#   - expected_category: None if absent or not a non-empty string.
#   - answer_category:   None if absent or not a non-empty string.
#   - granularity:       "general" (default) unless explicitly "specific".

from pipeline.code.pipeline import _parse_stage0_output


# ---- happy path: full 4-field JSON ----

def test_full_four_field_json_parses_all_fields():
    raw = (
        '{"entity":"warfarin",'
        '"expected_category":"biolink:Drug",'
        '"answer_category":"biolink:PhenotypicFeature",'
        '"granularity_preference":"specific"}'
    )
    out = _parse_stage0_output(raw)
    assert out.mention == "warfarin"
    assert out.expected_category == "biolink:Drug"
    assert out.answer_category == "biolink:PhenotypicFeature"
    assert out.granularity_preference == "specific"


# ---- legacy field-name aliases ----

# pre-2026-05 prompt called the entity field "name". some snapshot
# logs / older models still emit that. the parser MUST honour the
# alias — that's the whole point of having it.
def test_legacy_name_alias_is_honoured():
    raw = '{"name":"aspirin","expected_category":"biolink:ChemicalEntity"}'
    out = _parse_stage0_output(raw)
    assert out.mention == "aspirin"
    assert out.expected_category == "biolink:ChemicalEntity"


# pre-2026-05 prompt called expected_category "category". same logic.
def test_legacy_category_alias_is_honoured():
    raw = '{"entity":"HMGCR","category":"biolink:Gene"}'
    out = _parse_stage0_output(raw)
    assert out.mention == "HMGCR"
    assert out.expected_category == "biolink:Gene"


# ---- missing-field defaults ----

def test_missing_expected_category_yields_none():
    raw = '{"entity":"CFTR"}'
    out = _parse_stage0_output(raw)
    assert out.mention == "CFTR"
    assert out.expected_category is None
    assert out.answer_category is None


def test_missing_granularity_defaults_to_general():
    # default of "general" matches the prompt's "when unsure" rule
    raw = '{"entity":"seizures","expected_category":"biolink:PhenotypicFeature"}'
    out = _parse_stage0_output(raw)
    assert out.granularity_preference == "general"


def test_explicit_specific_is_preserved():
    raw = '{"entity":"warfarin","granularity_preference":"specific"}'
    out = _parse_stage0_output(raw)
    assert out.granularity_preference == "specific"


# only the literal string "specific" should switch to specific. any
# other value (typo, capitalised, garbage) reverts to "general" — this
# is fail-soft behaviour for a low-stakes preference.
def test_unknown_granularity_value_defaults_to_general():
    raw = '{"entity":"aspirin","granularity_preference":"detailed"}'
    out = _parse_stage0_output(raw)
    assert out.granularity_preference == "general"


# ---- plain-string fallback (legacy v15 pre-JSON output) ----

# older models / earlier prompts returned just the entity name as a bare
# string. parser falls back to using the whole text as the mention with
# no category info. this MUST keep working for replayed snapshot data.
def test_plain_string_falls_back_to_mention_only():
    out = _parse_stage0_output("warfarin")
    assert out.mention == "warfarin"
    assert out.expected_category is None
    assert out.answer_category is None
    assert out.granularity_preference == "general"


# ---- whitespace and quote tolerance ----

# the LLM occasionally wraps its single-string output in extra quotes,
# even when given a JSON contract. tolerate it.
def test_quoted_plain_string_strips_quotes():
    out = _parse_stage0_output('"warfarin"')
    assert out.mention == "warfarin"


# leading/trailing whitespace around JSON
def test_surrounding_whitespace_in_json_tolerated():
    raw = '   {"entity":"warfarin"}  \n'
    out = _parse_stage0_output(raw)
    assert out.mention == "warfarin"


# ---- empty / whitespace-only input ----

def test_empty_string_yields_empty_mention():
    # caller (run_grounded) will turn an empty mention into
    # STATUS_ENTITY_EMPTY. the parser must NOT raise — pipeline must
    # propagate the empty result, not crash.
    out = _parse_stage0_output("")
    assert out.mention == ""
    assert out.expected_category is None


def test_whitespace_only_yields_empty_mention():
    out = _parse_stage0_output("   \n\t  ")
    assert out.mention == ""


# ---- fenced JSON (LLM wraps in markdown) ----

# Stage 2 shares the same _extract_json under the hood, so fenced JSON
# should round-trip through _parse_stage0_output too.
def test_fenced_json_is_unwrapped():
    raw = '```json\n{"entity":"warfarin","expected_category":"biolink:Drug"}\n```'
    out = _parse_stage0_output(raw)
    assert out.mention == "warfarin"
    assert out.expected_category == "biolink:Drug"


# ---- defensive type coercion ----

# the LLM sometimes returns non-string values for the entity (e.g.,
# putting a number when given "1") — the parser coerces to str so the
# rest of the pipeline doesn't crash on a type error.
def test_non_string_entity_is_coerced_to_string():
    raw = '{"entity":42}'
    out = _parse_stage0_output(raw)
    assert out.mention == "42"
    assert isinstance(out.mention, str)
