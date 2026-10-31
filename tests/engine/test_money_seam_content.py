"""W3-06b-r1 — the money seam content: typed records, receipts, no arithmetic.

The frozen `UsageReceipt` shape is
`cloud/src/mcp/engine/schemas/usage-receipt.schema.json` at `rhobear-builds`
default branch: money is a string, never a float; unit counts are integers;
unknown fields are preserved; the state path is closed and one-way. The five
L2 calls are data — a request record carrying identity, and a response parser
that refuses a record naming another operation. Nothing here calls, stores,
charges, admits or reconciles.

The `next_state` calls below are written out literally, call by call; no test
imports a transition table from production, so deleting a production move
fails the test rather than shrinking it (the #57-r1 vacuity lesson).
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

from capturd.engine import l2, receipts
from capturd.engine.l2 import (
    L2Error,
    coverage_request,
    parse_coverage,
    parse_quote,
    parse_release,
    parse_reserve,
    parse_settle,
    quote_request,
    release_request,
    reserve_request,
    settle_request,
)
from capturd.engine.receipts import (
    REQUIRED_FIELDS,
    STATES,
    ReceiptError,
    is_metered,
    next_state,
    validate_coverage,
    validate_receipt,
)

OP = "op-0001"
ATTEMPT = "attempt-0001"
ACCOUNT = "acct-0001"
PRODUCT = "engine:capturd"
PRICE_VERSION = "prices-2026-10-31"
OCCURRED = "2026-10-31T12:00:00Z"

ENGINE_DIR = Path(__file__).resolve().parents[2] / "capturd" / "engine"
CONTENT_MODULES = ("l2.py", "receipts.py")
#: The explicit standard-library allowlist for the two content modules.
_ALLOWED_IMPORTS = {"__future__", "datetime", "re", "typing"}
_BANNED_IMPORTS = {
    "os", "pathlib", "time", "random", "uuid", "logging", "socket", "json",
    "subprocess", "shutil", "tempfile", "http", "urllib", "sqlite3",
}
_LOCAL_MODULES = {"receipts"}

COVERAGE = {
    "kind": "launch_mcp_unmetered",
    "startedAt": "2026-10-01T00:00:00Z",
    "endsAt": "2026-10-31T23:59:59+00:00",
    "seat": 7,
}


def make_receipt(**overrides):
    """A frozen-schema-shaped usage receipt; overrides stand in for drift."""
    record = {
        "operationId": OP,
        "attemptId": ATTEMPT,
        "accountId": ACCOUNT,
        "product": PRODUCT,
        "priceVersion": PRICE_VERSION,
        "units": {"searches": 1, "gpuSeconds": 0},
        "customerDebitUsd": "1234.50",
        "state": "reserved",
        "occurredAt": OCCURRED,
    }
    record.update(overrides)
    return record


# --------------------------------------------------------------------------- /
# 1. The boundary shape: required fields, money, units, the closed five


def test_required_fields_match_the_frozen_schema():
    assert tuple(REQUIRED_FIELDS) == (
        "operationId", "attemptId", "accountId", "product", "priceVersion",
        "units", "customerDebitUsd", "state", "occurredAt",
    )


@pytest.mark.parametrize("missing", (
    "operationId", "attemptId", "accountId", "product", "priceVersion",
    "units", "customerDebitUsd", "state", "occurredAt",
))
def test_receipt_requires_every_required_field(missing):
    record = make_receipt()
    del record[missing]
    with pytest.raises(ReceiptError):
        validate_receipt(record)


@pytest.mark.parametrize("bad", (1234.5, 1234, True, "-1"))
def test_money_is_a_string_never_a_float(bad):
    with pytest.raises(ReceiptError):
        validate_receipt(make_receipt(customerDebitUsd=bad))


def test_money_string_survives_byte_for_byte():
    record = make_receipt(customerDebitUsd="1234.50")
    validated = validate_receipt(record)
    assert validated["customerDebitUsd"] == "1234.50"
    assert record["customerDebitUsd"] == "1234.50"


def test_a_zero_charge_is_real_money_not_an_absence():
    zero = validate_receipt(make_receipt(customerDebitUsd="0.00"))
    covered = validate_receipt(make_receipt(customerDebitUsd=None, coverage=dict(COVERAGE)))
    assert zero["customerDebitUsd"] == "0.00"
    assert zero["customerDebitUsd"] is not None
    assert covered["customerDebitUsd"] is None


@pytest.mark.parametrize("bad", (1.0, True, -1, "3"))
def test_units_are_non_negative_integers(bad):
    with pytest.raises(ReceiptError):
        validate_receipt(make_receipt(units={"searches": bad}))


def test_zero_unit_counts_survive_as_integers():
    validated = validate_receipt(make_receipt(units={"searches": 0, "gpuSeconds": 0}))
    assert validated["units"] == {"searches": 0, "gpuSeconds": 0}
    assert all(isinstance(value, int) for value in validated["units"].values())


def test_state_is_the_closed_five():
    assert tuple(STATES) == ("reserved", "settled", "released", "unresolved", "reversed")
    for state in ("reserved", "settled", "released", "unresolved", "reversed"):
        assert validate_receipt(make_receipt(state=state))["state"] == state
    for state in ("waiting", "failed", "held", "Reserved", ""):
        with pytest.raises(ReceiptError):
            validate_receipt(make_receipt(state=state))


def test_settled_with_null_debit_is_valid_state_and_money_are_independent():
    settled = validate_receipt(make_receipt(state="settled", customerDebitUsd=None))
    assert settled["state"] == "settled"
    assert settled["customerDebitUsd"] is None
    assert is_metered(settled) is True


def test_validate_never_mutates_its_input():
    record = make_receipt(units={"searches": 2}, extra={"kept": True})
    before = copy.deepcopy(record)
    assert validate_receipt(record) == record
    assert record == before


def test_identity_boundaries():
    """The schema's 1-512 window is exact: both edges, both sides."""
    for field in ("operationId", "attemptId", "accountId", "product", "priceVersion"):
        assert validate_receipt(make_receipt(**{field: "x"}))[field] == "x"
        assert validate_receipt(make_receipt(**{field: "x" * 512}))[field] == "x" * 512
        for empty_or_over in ("", "x" * 513):
            with pytest.raises(ReceiptError):
                validate_receipt(make_receipt(**{field: empty_or_over}))


@pytest.mark.parametrize("impossible", (
    "2026-02-30T00:00:00Z",   # February has no 30th
    "2026-13-01T00:00:00Z",   # there is no 13th month
    "2026-10-31T25:00:00Z",   # there is no 25th hour
))
def test_an_impossible_timestamp_is_refused(impossible):
    """Regex-valid but impossible instants reach, and are refused by, the
    `fromisoformat` branch — the reason that call exists."""
    with pytest.raises(ReceiptError):
        validate_receipt(make_receipt(occurredAt=impossible))
    with pytest.raises(ReceiptError):
        validate_coverage({**COVERAGE, "startedAt": impossible})


# --------------------------------------------------------------------------- /
# 2. The closed one-way state path — literal calls, no production table


@pytest.mark.parametrize("current,event,expected", (
    ("reserved", "settle", "settled"),
    ("reserved", "release", "released"),
    ("settled", "reverse", "reversed"),
))
def test_next_state_moves(current, event, expected):
    assert next_state(current, event) == expected


@pytest.mark.parametrize("current,event", (
    ("reserved", "reserve"), ("reserved", "reverse"),
    ("settled", "reserve"), ("settled", "settle"), ("settled", "release"),
    ("released", "reserve"), ("released", "settle"), ("released", "release"),
    ("released", "reverse"),
    ("unresolved", "reserve"), ("unresolved", "settle"), ("unresolved", "release"),
    ("unresolved", "reverse"),
    ("reversed", "reserve"), ("reversed", "settle"), ("reversed", "release"),
    ("reversed", "reverse"),
    (None, "reserve"), (None, "settle"), (None, "release"), (None, "reverse"),
    ("waiting", "settle"), ("failed", "settle"), ("", "reserve"),
    ("reserved", "bogus"), (None, "bogus"),
))
def test_next_state_refuses_every_other_pair(current, event):
    with pytest.raises(ReceiptError):
        next_state(current, event)


def test_unresolved_is_not_reachable_from_next_state():
    produced = {
        next_state("reserved", "settle"),
        next_state("reserved", "release"),
        next_state("settled", "reverse"),
    }
    assert produced == {"settled", "released", "reversed"}
    assert "unresolved" not in produced
    for event in ("reserve", "settle", "release", "reverse"):
        with pytest.raises(ReceiptError):
            next_state("unresolved", event)


def test_next_state_documents_only_what_it_does():
    """The three pairs move; `None` has no move — the caller creates the first
    state — and `unresolved` is unreachable from every state."""
    assert next_state("reserved", "settle") == "settled"
    assert next_state("reserved", "release") == "released"
    assert next_state("settled", "reverse") == "reversed"
    with pytest.raises(ReceiptError):
        next_state(None, "reserve")
    produced = {next_state("reserved", "settle"), next_state("reserved", "release"),
                next_state("settled", "reverse")}
    assert "unresolved" not in produced
    for state in STATES:
        with pytest.raises(ReceiptError):
            next_state(state, "reserve")


# --------------------------------------------------------------------------- /
# 3. Unknown fields, coverage, the exempt versus refused distinction


def test_unknown_fields_are_preserved():
    record = make_receipt(State="settled", extra={"kept": True})
    validated = validate_receipt(record)
    assert validated["State"] == "settled"
    assert validated["extra"] == {"kept": True}
    assert validated["state"] == "reserved"
    miscased = make_receipt()
    del miscased["state"]
    miscased["State"] = "reserved"
    with pytest.raises(ReceiptError):
        validate_receipt(miscased)


def test_coverage_block_is_validated_when_present():
    covered = validate_receipt(make_receipt(customerDebitUsd=None, coverage=dict(COVERAGE)))
    assert covered["coverage"]["seat"] == 7
    assert covered["customerDebitUsd"] is None
    assert is_metered(covered) is False
    assert is_metered(make_receipt()) is True

    malformed = (
        {**COVERAGE, "kind": "trial"},
        {key: value for key, value in COVERAGE.items() if key != "endsAt"},
        {**COVERAGE, "seat": "7"},
        {**COVERAGE, "seat": True},
        {**COVERAGE, "endsAt": "2026-09-30T00:00:00Z"},
        {**COVERAGE, "startedAt": "not a timestamp"},
        None,
        "launch_mcp_unmetered",
    )
    for block in malformed:
        with pytest.raises(ReceiptError):
            validate_receipt(make_receipt(customerDebitUsd=None, coverage=block))


def test_is_metered_never_answers_for_an_untrusted_record():
    with pytest.raises(ReceiptError):
        is_metered(make_receipt(state="waiting"))


# --------------------------------------------------------------------------- /
# 4. The five L2 calls as data: identity in, identity echoed, else L2Error


CALLS = (
    ("quote", quote_request, parse_quote,
     dict(operationId=OP, accountId=ACCOUNT, product=PRODUCT,
          units={"searches": 1}, priceVersion=PRICE_VERSION),
     dict(priceVersion=PRICE_VERSION, requiredUsd="0.10")),
    ("reserve", reserve_request, parse_reserve,
     dict(operationId=OP, attemptId=ATTEMPT, accountId=ACCOUNT, product=PRODUCT,
          units={"searches": 1}, maxUsd="1.00", priceVersion=PRICE_VERSION),
     dict(reservationId="res-1", priceVersion=PRICE_VERSION, heldUsd="1.00")),
    ("settle", settle_request, parse_settle,
     dict(operationId=OP, attemptId=ATTEMPT, units={"searches": 1}),
     dict(receiptId="l2rc-res-1-1", customerDebitUsd="0.10")),
    ("release", release_request, parse_release,
     dict(operationId=OP, attemptId=ATTEMPT),
     dict(released=True, reason=None)),
    ("coverage", coverage_request, parse_coverage,
     dict(operationId=OP, attemptId=ATTEMPT, accountId=ACCOUNT, product=PRODUCT),
     dict(kind="launch_mcp_unmetered",
          startedAt="2026-10-01T00:00:00Z", endsAt="2026-10-31T23:59:59Z")),
)
CALL_IDS = [name for name, *_ in CALLS]


@pytest.mark.parametrize("name,build,parse,request_args,answer_fields", CALLS, ids=CALL_IDS)
def test_each_request_carries_its_identity(name, build, parse, request_args, answer_fields):
    request = build(**request_args)
    assert request["operationId"] == OP
    if "attemptId" in request_args:
        assert request["attemptId"] == ATTEMPT


@pytest.mark.parametrize("name,build,parse,request_args,answer_fields", CALLS, ids=CALL_IDS)
def test_a_response_naming_another_operation_is_refused(name, build, parse,
                                                        request_args, answer_fields):
    request = build(**request_args)
    answer = {"operationId": OP, "attemptId": ATTEMPT, **answer_fields}
    parsed = parse(answer, request)
    assert parsed["operationId"] == OP
    with pytest.raises(L2Error):
        parse({**answer, "operationId": "op-other"}, request)
    if "attemptId" in request:
        with pytest.raises(L2Error):
            parse({**answer, "attemptId": "attempt-other"}, request)


def test_a_response_keeps_fields_the_seam_does_not_know():
    request = quote_request(operationId=OP, accountId=ACCOUNT, product=PRODUCT,
                            units={"searches": 1}, priceVersion=PRICE_VERSION)
    answer = {"operationId": OP, "priceVersion": PRICE_VERSION, "requiredUsd": "0.10",
              "futureField": {"kept": True}}
    assert parse_quote(answer, request)["futureField"] == {"kept": True}


def _money_request(argument, value):
    """The two request calls that carry money, exercised one keyword at a time."""
    if argument == "maxUsd":
        return reserve_request(operationId=OP, attemptId=ATTEMPT, accountId=ACCOUNT,
                               product=PRODUCT, units={"searches": 1},
                               maxUsd=value, priceVersion=PRICE_VERSION)
    assert argument == "providerCostUsd"
    return settle_request(operationId=OP, attemptId=ATTEMPT, units={"searches": 1},
                          providerCostUsd=value)


@pytest.mark.parametrize("argument", ("maxUsd", "providerCostUsd"))
def test_a_request_refuses_non_string_money(argument):
    """No float, int, bool, comma-decimal, negative or empty money leaves."""
    for bad in (1.0, 1, True, "1,00", "-1", ""):
        with pytest.raises(L2Error):
            _money_request(argument, bad)
    for good in ("0.00", "12.34"):
        assert _money_request(argument, good)[argument] == good


def test_settle_request_without_a_provider_cost_keeps_the_key_absent():
    request = settle_request(operationId=OP, attemptId=ATTEMPT, units={"searches": 1})
    assert "providerCostUsd" not in request


def test_a_settle_answer_without_the_debit_field_is_refused():
    """A missing `customerDebitUsd` is not an answer; an explicit null is."""
    request = settle_request(operationId=OP, attemptId=ATTEMPT, units={"searches": 1})
    truncated = {"operationId": OP, "attemptId": ATTEMPT, "receiptId": "l2rc-res-1-1"}
    with pytest.raises(L2Error):
        parse_settle(truncated, request)
    covered = {**truncated, "customerDebitUsd": None}
    assert parse_settle(covered, request)["customerDebitUsd"] is None


def test_a_request_record_does_not_alias_the_units_it_was_given():
    units = {"searches": 2}
    request = quote_request(operationId=OP, accountId=ACCOUNT, product=PRODUCT,
                            units=units, priceVersion=PRICE_VERSION)
    units["searches"] = 99
    assert request["units"] == {"searches": 2}


def test_parsers_refuse_a_wrong_shaped_answer():
    quote = quote_request(operationId=OP, accountId=ACCOUNT, product=PRODUCT,
                          units={"searches": 1}, priceVersion=PRICE_VERSION)
    for bad in (None, 0.10, 10, "-1", ""):
        with pytest.raises(L2Error):
            parse_quote({"operationId": OP, "priceVersion": PRICE_VERSION,
                         "requiredUsd": bad}, quote)

    reserve = reserve_request(operationId=OP, attemptId=ATTEMPT, accountId=ACCOUNT,
                              product=PRODUCT, units={"searches": 1},
                              maxUsd="1.00", priceVersion=PRICE_VERSION)
    with pytest.raises(L2Error):
        parse_reserve({"operationId": OP, "attemptId": ATTEMPT, "reservationId": "res-1",
                       "priceVersion": PRICE_VERSION, "heldUsd": 1.00}, reserve)

    settle = settle_request(operationId=OP, attemptId=ATTEMPT, units={"searches": 1})
    with pytest.raises(L2Error):
        parse_settle({"operationId": OP, "attemptId": ATTEMPT, "receiptId": "l2rc-1",
                      "customerDebitUsd": 0.10}, settle)
    assert parse_settle({"operationId": OP, "attemptId": ATTEMPT, "receiptId": "l2rc-1",
                         "customerDebitUsd": None}, settle)["customerDebitUsd"] is None

    release = release_request(operationId=OP, attemptId=ATTEMPT)
    for bad in (1, "true", None, False):
        with pytest.raises(L2Error):
            parse_release({"operationId": OP, "attemptId": ATTEMPT, "released": bad}, release)

    coverage = coverage_request(operationId=OP, attemptId=ATTEMPT, accountId=ACCOUNT,
                                product=PRODUCT)
    with pytest.raises(L2Error):
        parse_coverage({"operationId": OP, "attemptId": ATTEMPT, "kind": "trial",
                        **{key: COVERAGE[key] for key in ("startedAt", "endsAt")}}, coverage)
    assert parse_coverage(None, coverage) is None


# --------------------------------------------------------------------------- /
# 5. Purity and the no-arithmetic proof


def test_modules_stay_pure():
    for name in CONTENT_MODULES:
        source = (ENGINE_DIR / name).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in _BANNED_IMPORTS, f"{name}: banned import {alias.name}"
                    assert alias.name in _ALLOWED_IMPORTS, \
                        f"{name}: import outside allowlist: {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    assert node.level == 1, f"{name}: relative import level {node.level}"
                    assert node.module in _LOCAL_MODULES, \
                        f"{name}: relative import outside the engine content: {node.module}"
                else:
                    root = (node.module or "").split(".")[0]
                    assert root not in _BANNED_IMPORTS, \
                        f"{name}: banned import from {node.module}"
                    assert node.module in _ALLOWED_IMPORTS, \
                        f"{name}: import outside allowlist: {node.module}"
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    assert func.id not in {"open", "__import__", "eval", "exec", "compile"}, \
                        f"{name}: a {func.id}() call"
                elif isinstance(func, ast.Attribute):
                    assert func.attr not in {
                        "open", "now", "today", "utcnow", "time", "monotonic",
                        "sleep", "getenv",
                    }, f"{name}: a .{func.attr}() call"


def test_no_arithmetic_on_money():
    """No two values — money above all — are ever combined by arithmetic."""
    offenders = []
    for name in CONTENT_MODULES:
        source = (ENGINE_DIR / name).read_text(encoding="utf-8")
        assert "customerDebitUsd" in source, f"{name}: no money field to watch"
        tree = ast.parse(source, filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(
                    node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
                              ast.Mod, ast.Pow)):
                offenders.append(f"{name}:{node.lineno}:{type(node.op).__name__}")
    assert not offenders, f"arithmetic in the money seam: {offenders}"


# --------------------------------------------------------------------------- /
# 6. W3-06b-r3 — the builders guard the whole record; null and absence agree


def _quote(**overrides):
    args = dict(operationId=OP, accountId=ACCOUNT, product=PRODUCT,
                units={"searches": 1}, priceVersion=PRICE_VERSION)
    args.update(overrides)
    return quote_request(**args)


def _reserve(**overrides):
    args = dict(operationId=OP, attemptId=ATTEMPT, accountId=ACCOUNT, product=PRODUCT,
                units={"searches": 1}, maxUsd="1.00", priceVersion=PRICE_VERSION)
    args.update(overrides)
    return reserve_request(**args)


def _settle(**overrides):
    args = dict(operationId=OP, attemptId=ATTEMPT, units={"searches": 1})
    args.update(overrides)
    return settle_request(**args)


def _release(**overrides):
    args = dict(operationId=OP, attemptId=ATTEMPT)
    args.update(overrides)
    return release_request(**args)


def _coverage(**overrides):
    args = dict(operationId=OP, attemptId=ATTEMPT, accountId=ACCOUNT, product=PRODUCT)
    args.update(overrides)
    return coverage_request(**args)


#: name, builder, the identity fields it carries, whether it carries units.
BUILDERS = (
    ("quote", _quote, ("operationId", "accountId", "product", "priceVersion"), True),
    ("reserve", _reserve,
     ("operationId", "attemptId", "accountId", "product", "priceVersion"), True),
    ("settle", _settle, ("operationId", "attemptId"), True),
    ("release", _release, ("operationId", "attemptId"), False),
    ("coverage", _coverage, ("operationId", "attemptId", "accountId", "product"), False),
)
BUILDERS_BY_NAME = {name: build for name, build, _, _ in BUILDERS}

#: name, parser, request builder, a valid dict answer, and the parser's own
#: required text fields. Release has no required text field, so no entry here.
PARSER_CASES = (
    ("quote", parse_quote, _quote,
     dict(operationId=OP, priceVersion=PRICE_VERSION, requiredUsd="0.10")),
    ("reserve", parse_reserve, _reserve,
     dict(operationId=OP, attemptId=ATTEMPT, reservationId="res-1",
          priceVersion=PRICE_VERSION, heldUsd="1.00")),
    ("settle", parse_settle, _settle,
     dict(operationId=OP, attemptId=ATTEMPT, receiptId="l2rc-1", customerDebitUsd=None)),
    ("release", parse_release, _release,
     dict(operationId=OP, attemptId=ATTEMPT, released=True)),
    ("coverage", parse_coverage, _coverage,
     dict(operationId=OP, attemptId=ATTEMPT, kind="launch_mcp_unmetered",
          startedAt="2026-10-01T00:00:00Z", endsAt="2026-10-31T23:59:59Z")),
)
PARSER_IDS = [case[0] for case in PARSER_CASES]
TEXT_FIELD_CASES = [
    ("quote", parse_quote, _quote, PARSER_CASES[0][3], "priceVersion"),
    ("reserve", parse_reserve, _reserve, PARSER_CASES[1][3], "reservationId"),
    ("reserve", parse_reserve, _reserve, PARSER_CASES[1][3], "priceVersion"),
    ("settle", parse_settle, _settle, PARSER_CASES[2][3], "receiptId"),
    ("coverage", parse_coverage, _coverage, PARSER_CASES[4][3], "kind"),
]


def _bad_builder_cases():
    """Every builder against every bad record it can actually be handed."""
    cases = []
    for name, _build, identity_fields, has_units in BUILDERS:
        for field in identity_fields:
            cases.append(pytest.param(name, field, "", id=f"{name}-{field}-empty"))
            cases.append(pytest.param(name, field, "x" * 513, id=f"{name}-{field}-513"))
        if has_units:
            for bad in (1.5, True, -1):
                cases.append(pytest.param(name, "units", bad, id=f"{name}-units-{bad!r}"))
    cases.append(pytest.param("release", "reason", 42, id="release-reason-42"))
    cases.append(pytest.param("release", "reason", 1.5, id="release-reason-1.5"))
    return cases


@pytest.mark.parametrize("name,field,bad", _bad_builder_cases())
def test_every_builder_refuses_a_bad_record(name, field, bad):
    """Item 1: the builders guard the whole record, not only money."""
    build = BUILDERS_BY_NAME[name]
    if field == "units":
        with pytest.raises(L2Error):
            build(units={"searches": bad})
    else:
        with pytest.raises(L2Error):
            build(**{field: bad})


def test_builder_identity_window_is_the_schema_window():
    """The same 1-512 rule receipts.py enforces, newline behaviour included."""
    for name, build, identity_fields, _ in BUILDERS:
        for field in identity_fields:
            assert build(**{field: "x"})[field] == "x"
            assert build(**{field: "x" * 512})[field] == "x" * 512
            with pytest.raises(L2Error):
                build(**{field: "x" * 513})
            with pytest.raises(L2Error):
                build(**{field: ""})
    # The pinned schema pattern is ^.{1,512}$ with no newline mode: an
    # embedded newline is outside the window on both the builder and the
    # receipt. Do not "fix" this to diverge from the pinned rule.
    with pytest.raises(L2Error):
        _quote(operationId="a\nb")
    with pytest.raises(ReceiptError):
        validate_receipt(make_receipt(operationId="a\nb"))


def test_builder_unit_edges_are_counts_not_absences():
    assert _quote(units={})["units"] == {}
    assert _quote(units={"searches": 0})["units"] == {"searches": 0}
    with pytest.raises(L2Error):
        _quote(units=5)


@pytest.mark.parametrize("name,parse,build,answer", PARSER_CASES, ids=PARSER_IDS)
@pytest.mark.parametrize("bad", (None, [], "answer"))
def test_every_parser_refuses_a_non_dict_answer(name, parse, build, answer, bad):
    """Item 2's first guard branch, driven on every parser."""
    request = build()
    if parse is parse_coverage and bad is None:
        # None is the documented no-coverage answer for this parser alone.
        assert parse(None, request) is None
        return
    with pytest.raises(L2Error):
        parse(bad, request)


@pytest.mark.parametrize("name,parse,build,answer,field", TEXT_FIELD_CASES,
                         ids=[f"{case[0]}-{case[4]}" for case in TEXT_FIELD_CASES])
@pytest.mark.parametrize("spelling", ("absent", "empty"))
def test_every_parser_refuses_a_missing_or_empty_text_field(name, parse, build, answer,
                                                            field, spelling):
    """Item 2's second guard branch: every required text field is proven."""
    request = build()
    broken = dict(answer)
    if spelling == "absent":
        del broken[field]
    else:
        broken[field] = ""
    with pytest.raises(L2Error):
        parse(broken, request)


@pytest.mark.parametrize("name,parse,build,answer", PARSER_CASES, ids=PARSER_IDS)
def test_a_request_without_identity_is_refused_not_crashed(name, parse, build, answer):
    """Item 2: a hand-built request refuses with L2Error, never KeyError."""
    for bad_request in ({}, {"operationId": 42}, {"operationId": None},
                        {"operationId": ""}, {"operationId": "x" * 513}):
        with pytest.raises(L2Error):
            parse(answer, bad_request)
    with pytest.raises(L2Error):
        parse(answer, None)


def test_fractional_seconds_are_runtime_independent():
    """Item 3, pinned-regex option: the module, not the interpreter, decides.

    The fraction is pinned to the widths the minimum supported runtime
    (`requires-python >= 3.10`) parses — exactly three or six digits — so the
    accepted and refused spellings below hold on every runtime. There is no
    `sys.version_info` here: this is a property of the module.
    """
    for digits in ("123", "123456"):
        stamp = f"2026-10-31T12:00:00.{digits}Z"
        assert validate_receipt(make_receipt(occurredAt=stamp))["occurredAt"] == stamp
        assert validate_coverage({**COVERAGE, "startedAt": stamp})["startedAt"] == stamp
    for digits in ("1", "12", "1234", "12345", "1234567"):
        with pytest.raises(ReceiptError):
            validate_receipt(make_receipt(occurredAt=f"2026-10-31T12:00:00.{digits}Z"))
        with pytest.raises(ReceiptError):
            validate_coverage({**COVERAGE, "startedAt": f"2026-10-01T00:00:00.{digits}Z"})
    # No fraction and `Z` both still validate, on receipts and coverage.
    for stamp in ("2026-10-31T12:00:00Z", "2026-10-31T12:00:00+00:00",
                  "2026-10-31T12:00:00.123+00:00"):
        assert validate_receipt(make_receipt(occurredAt=stamp))["occurredAt"] == stamp


def test_null_and_absence_mean_the_same_thing_on_both_halves():
    """Item 4 decision: one mapping, asserted on both halves at once.

    Request side: `providerCostUsd=None` states an explicit null; omitting the
    keyword, or passing the documented `PROVIDER_COST_ABSENT` sentinel, leaves
    the key out. Answer side: an explicit `customerDebitUsd: null` parses as
    covered; an absent key is refused. Neither half invents a third meaning
    for either spelling.
    """
    stated = _settle(providerCostUsd=None)
    omitted = _settle()
    sentinel_absent = _settle(providerCostUsd=l2.PROVIDER_COST_ABSENT)
    assert stated["providerCostUsd"] is None
    assert "providerCostUsd" not in omitted
    assert sentinel_absent == omitted

    answer = {"operationId": OP, "attemptId": ATTEMPT, "receiptId": "l2rc-1"}
    assert parse_settle({**answer, "customerDebitUsd": None},
                        stated)["customerDebitUsd"] is None
    with pytest.raises(L2Error):
        parse_settle(dict(answer), sentinel_absent)
