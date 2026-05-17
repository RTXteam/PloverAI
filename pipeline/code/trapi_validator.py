# trapi_validator.py — wraps NCATSTranslator's reasoner-validator (6.x).
# the rest of the pipeline calls a single function and gets a typed
# result. this is the v15 "validation policy" gate: invalid TRAPI ->
# the run stops; we never auto-repair the query.

from __future__ import annotations

# logging: stdlib. logger injected so the validator's pass/fail line
# shows up in the same per-run log as every other stage.
import logging

# dataclasses: stdlib. ValidationResult is a frozen dataclass that
# pipeline.py reads `passed` off and writes `raw` to disk verbatim.
from dataclasses import dataclass

# typing.Any: stdlib. the validator's report is a deeply nested dict
# whose inner shape lives in reasoner-validator's own docs; we
# preserve it as-is rather than re-type it ourselves.
from typing import Any


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    errors: dict[str, Any]
    warnings: dict[str, Any]
    info: dict[str, Any]
    raw: dict[str, Any]   # the full validator report, kept for the trace


def validate_query(
    trapi_message: dict[str, Any],
    *,
    trapi_version: str = "1.5.0",
    biolink_version: str = "4.2.5",
    logger: logging.Logger,
) -> ValidationResult:
    # reasoner-validator imports heavy Biolink machinery on first call,
    # so we lazy-import it here. that keeps `runner.py --help` snappy
    # and means a missing optional dep doesn't crash module import.
    from reasoner_validator.validator import TRAPIGraphType, TRAPIResponseValidator

    v = TRAPIResponseValidator(
        trapi_version=trapi_version,
        biolink_version=biolink_version,
    )

    # we are validating a QUERY graph (what the LLM built), not a
    # response. reasoner-validator 6.x exposes
    # check_biolink_model_compliance for this; the older
    # check_compliance_of_trapi_response expects a full response shape.
    qg = trapi_message.get("message", {}).get("query_graph", {})
    v.check_biolink_model_compliance(
        graph=qg,
        graph_type=TRAPIGraphType.Query_Graph,
    )

    errors = v.get_errors() or {}
    warnings = v.get_warnings() or {}
    # we don't pull "info" messages separately. reasoner-validator's
    # get_all_messages_of_type() expects a MessageType enum (not a
    # string), and the API drifts between minor versions. info-level
    # diagnostics aren't actionable for our v15 gate anyway — they're
    # in the full report we keep in `raw` if anyone needs them.
    info: dict[str, Any] = {}
    raw = v.get_all_messages() or {}

    passed = not v.has_errors()
    if passed:
        # warning count counted across all (test, target) pairs in the
        # nested dict — we just stringify length so the live log stays
        # short. detailed warnings are in validation.json.
        n_warn = sum(len(x) for x in warnings.values()) if isinstance(warnings, dict) else 0
        logger.info(
            f"[bold green]✓ validator[/]  passed  warnings={n_warn}"
        )
    else:
        # show the first error as a short string. the full structured
        # error map ends up in validation.json for post-mortem.
        first = next(iter(errors)) if errors else "<unknown>"
        n_err = sum(len(x) for x in errors.values()) if isinstance(errors, dict) else 0
        logger.warning(
            f"[bold red]✗ validator[/]  failed  errors={n_err}  "
            f"first=[red]{str(first)[:200]}[/]"
        )

    return ValidationResult(
        passed=passed,
        errors=errors if isinstance(errors, dict) else {},
        warnings=warnings if isinstance(warnings, dict) else {},
        info=info if isinstance(info, dict) else {},
        raw=raw if isinstance(raw, dict) else {"raw": raw},
    )
