# _extract_json: tolerant JSON extraction from LLM output.
#
# the LLM is told "no markdown fences, no commentary" but ~5-10% of
# responses still wrap JSON in ```json fences or add a sentence before
# / after. this function is the one place that handles that variance
# for every JSON-shaped stage (scope check, entity extract, candidate
# pick, TRAPI build). it is pure (no IO) and used in 4+ stages, so a
# regression here breaks the whole pipeline silently.
#
# behaviour spec (in priority order):
#   1. plain JSON object  → parse normally
#   2. JSON in ```json...``` or ``` ... ``` fences → strip fences, parse
#   3. JSON embedded in prose → extract the largest {...} block, parse
#   4. genuinely non-JSON → raise json.JSONDecodeError (loud failure)
#   5. empty input → raise

import json

import pytest

from pipeline.code.pipeline import _extract_json


# ---- the happy path ----

def test_plain_json_object_parses() -> None:
    out = _extract_json('{"a": 1, "b": "two"}')
    assert out == {"a": 1, "b": "two"}


# ---- fence stripping (the most common LLM deviation) ----

def test_json_in_json_fence_parses() -> None:
    # markdown ```json``` is the most common fence the LLM emits
    raw = '```json\n{"in_scope": false, "reason": "test"}\n```'
    out = _extract_json(raw)
    assert out == {"in_scope": False, "reason": "test"}


def test_json_in_bare_fence_parses() -> None:
    # ``` without `json` after it is also common
    raw = '```\n{"x": 42}\n```'
    out = _extract_json(raw)
    assert out == {"x": 42}


def test_fence_with_trailing_whitespace() -> None:
    # the LLM may put extra whitespace/newlines around the fence —
    # both leading and trailing must be tolerated
    raw = '   ```json   \n{"k": "v"}\n```   \n'
    out = _extract_json(raw)
    assert out == {"k": "v"}


# ---- embedded JSON (LLM adds commentary before/after) ----

def test_json_embedded_in_prose_extracts_largest_object() -> None:
    # the fallback path: find the largest {...} block via regex. used
    # when the LLM ignores "no commentary" and writes a sentence.
    raw = 'Sure! Here is the answer: {"chosen_curie": "MONDO:0005148", "reason": "the question matches type 2 diabetes"} Hope this helps.'
    out = _extract_json(raw)
    assert out == {"chosen_curie": "MONDO:0005148", "reason": "the question matches type 2 diabetes"}


# ---- the loud-failure path ----

def test_non_json_raises_json_decode_error() -> None:
    # if there's no JSON anywhere, we want a loud failure (caller decides
    # whether to fail open or fail closed). the function must NOT silently
    # return {} or None.
    with pytest.raises(json.JSONDecodeError):
        _extract_json("this is not json at all")


def test_completely_empty_string_raises() -> None:
    with pytest.raises(json.JSONDecodeError):
        _extract_json("")


def test_unbalanced_braces_raises() -> None:
    # this is the one we deliberately want to fail. "{a: 1" is not parseable.
    # the regex would match nothing meaningful. assert loud failure.
    with pytest.raises(json.JSONDecodeError):
        _extract_json("{not actually json")


# ---- nested objects ----

def test_nested_object_parses() -> None:
    # multi-level nesting must survive both the fence and regex paths.
    # mirrors the shape of a TRAPI query_graph.
    raw = '```json\n{"a": {"b": {"c": [1, 2, 3]}}}\n```'
    out = _extract_json(raw)
    assert out == {"a": {"b": {"c": [1, 2, 3]}}}


# ---- the "JSON-in-prose with nested braces" case ----

def test_regex_fallback_picks_outermost_block() -> None:
    # the regex `\{.*\}` with DOTALL is greedy — it should grab from the
    # first `{` to the LAST `}`. test this with a nested object embedded
    # in prose so the inner `}` doesn't terminate matching prematurely.
    raw = 'response: {"outer": {"inner": "value"}} done.'
    out = _extract_json(raw)
    assert out == {"outer": {"inner": "value"}}


def test_trailing_prose_after_object_is_ignored() -> None:
    assert _extract_json('{"a": 1}\n\nHere is why I chose this.') == {"a": 1}


def test_second_object_after_first_does_not_break_parsing() -> None:
    assert _extract_json('{"a": {"b": 1}} {"a": 2}') == {"a": {"b": 1}}


def test_leading_prose_before_object() -> None:
    assert _extract_json('Sure! {"answers": []}') == {"answers": []}


def test_missing_trailing_brace_is_repaired() -> None:
    # 13-slug smoke, ling-3.0-flash: valid query graph short by one brace
    text = '{"message": {"query_graph": {"edges": {"e0": {"subject": "n0"}}}}'
    assert _extract_json(text)["message"]["query_graph"]["edges"]["e0"]["subject"] == "n0"


def test_more_than_three_missing_braces_still_fails() -> None:
    import json as _json
    import pytest
    with pytest.raises(_json.JSONDecodeError):
        _extract_json('{"a": {"b": {"c": {"d": {"e": 1')


def test_top_level_array_rejected_by_default() -> None:
    import json as _json
    import pytest
    with pytest.raises(_json.JSONDecodeError):
        _extract_json('[{"curie": "CHEBI:6801"}]')


def test_top_level_array_wrapped_as_answers_when_allowed() -> None:
    # 13-slug smoke, ling-3.0-flash returned a bare array at stage 11
    out = _extract_json('[{"curie": "CHEBI:6801"}, "CHEBI:5441"]', allow_list=True)
    assert out == {"answers": [{"curie": "CHEBI:6801"}, "CHEBI:5441"]}


def test_scalar_json_rejected() -> None:
    import json as _json
    import pytest
    with pytest.raises(_json.JSONDecodeError):
        _extract_json('42')
