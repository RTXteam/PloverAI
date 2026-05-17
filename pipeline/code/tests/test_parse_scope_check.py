# _parse_scope_check_output: parser for Stage 1's (in_scope, reason) JSON.
#
# the critical, non-obvious behaviour to lock down here is FAIL-OPEN:
# if the LLM's scope-check output is malformed in any way (bad JSON,
# missing field, wrong type, etc.), the parser must return
# in_scope=True so the pipeline runs anyway. blocking a real biomedical
# question because Stage 1's OWN output was malformed is worse than
# letting one chit-chat through to be handled downstream.
#
# this is the spec these tests pin down.

from code.pipeline import _parse_scope_check_output


# ---- happy path: well-formed responses ----

def test_in_scope_true_with_empty_reason():
    out = _parse_scope_check_output('{"in_scope": true, "reason": ""}')
    assert out.in_scope is True
    assert out.reason == ""


def test_in_scope_false_with_reason_preserved():
    raw = '{"in_scope": false, "reason": "this is a policy question"}'
    out = _parse_scope_check_output(raw)
    assert out.in_scope is False
    assert out.reason == "this is a policy question"


# ---- THE CRITICAL FAIL-OPEN BEHAVIOURS ----

# bad JSON → fail open. this is the one we cannot get wrong: a malformed
# guardrail response must never block a real question.
def test_malformed_json_fails_open():
    out = _parse_scope_check_output("not valid json")
    assert out.in_scope is True
    assert out.reason == ""


def test_partial_json_fails_open():
    # truncated JSON object
    out = _parse_scope_check_output('{"in_scope": false, "reason":')
    assert out.in_scope is True


def test_empty_string_fails_open():
    out = _parse_scope_check_output("")
    assert out.in_scope is True
    assert out.reason == ""


def test_whitespace_only_fails_open():
    out = _parse_scope_check_output("   \n\t   ")
    assert out.in_scope is True


# wrong type for in_scope (string instead of bool) → coerced safely.
# the prompt says return a bool; a snapshot LLM might emit "false" as
# a string. we treat that as the "wrong type" branch which falls back
# to True (fail open).
def test_string_in_scope_value_fails_open():
    raw = '{"in_scope": "false", "reason": "test"}'
    out = _parse_scope_check_output(raw)
    # string "false" is not a real bool — parser falls back to True.
    # this is intentional: if the LLM doesn't follow the contract,
    # err toward running the pipeline.
    assert out.in_scope is True


# missing in_scope field entirely → fail open
def test_missing_in_scope_field_fails_open():
    raw = '{"reason": "ambiguous"}'
    out = _parse_scope_check_output(raw)
    assert out.in_scope is True


# ---- fence stripping (shared with other JSON stages) ----

def test_fenced_json_unwraps_correctly():
    raw = '```json\n{"in_scope": false, "reason": "math problem"}\n```'
    out = _parse_scope_check_output(raw)
    assert out.in_scope is False
    assert out.reason == "math problem"


# ---- defensive: non-string reason ----

def test_non_string_reason_becomes_empty_string():
    # if the LLM puts a number in `reason`, downstream code expects a
    # string. parser coerces / drops.
    raw = '{"in_scope": false, "reason": 42}'
    out = _parse_scope_check_output(raw)
    # in_scope was a real bool, so it's respected
    assert out.in_scope is False
    # but reason wasn't a string, so it gets dropped to ""
    assert out.reason == ""


# ---- reason whitespace trimming ----

def test_reason_is_stripped():
    raw = '{"in_scope": false, "reason": "   trimmed   "}'
    out = _parse_scope_check_output(raw)
    assert out.reason == "trimmed"


# ---- the negative case: confirmed-out-of-scope is preserved verbatim ----

# locking the contract: when the LLM REALLY says out-of-scope, we
# preserve in_scope=False (no accidental fail-open). this is the
# inverse pin against an over-eager "always fail open" refactor.
def test_explicit_in_scope_false_is_respected():
    raw = '{"in_scope": false, "reason": "general world knowledge"}'
    out = _parse_scope_check_output(raw)
    assert out.in_scope is False
    assert out.reason == "general world knowledge"
