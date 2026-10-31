"""The five L2 calls as data: request records with identity, response parsers
that refuse a record naming another operation. The injected client is the
control plane's `l2-client.mjs` at `rhobear-builds`; no money arithmetic.

Absence and null are one vocabulary on both halves: `None` states an explicit
null, and the `PROVIDER_COST_ABSENT` sentinel on `settle_request` states that
the `providerCostUsd` key is not part of the record at all.
"""

from __future__ import annotations

from typing import Any

from .receipts import ReceiptError, is_identity, is_money_string, validate_coverage


class L2Error(ValueError):
    """The client answered with a record that is not the call's shape."""


class _Absent:
    """The documented sentinel: the money key is not stated at all."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "PROVIDER_COST_ABSENT"


#: Pass as `providerCostUsd` to leave the key out of a `settle_request`;
#: passing `None` instead states an explicit `null` (covered).
PROVIDER_COST_ABSENT = _Absent()


def _check(record: dict[str, Any], *,
           nullable_money: tuple[str, ...] = ()) -> dict[str, Any]:
    """The one guard every builder runs on the record it just built.

    Identity fields follow the same 1-512 rule `receipts.py` enforces; every
    unit count is a non-negative integer and never a bool; every money field is
    a string, except a field in `nullable_money`, which may also be an explicit
    `None`; a `reason`, when present, is a string. Refuses with `L2Error`.
    """
    for field in ("operationId", "attemptId", "accountId", "product", "priceVersion"):
        value = record.get(field)
        if field in record and not is_identity(value):
            raise L2Error(f"{field} must be a 1-512 character string, not {value!r}")
    if "units" in record:
        units = record.get("units")
        if not isinstance(units, dict):
            raise L2Error(f"units must be an object, not {type(units).__name__}")
        checked: dict[str, int] = {}
        for name, count in units.items():
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise L2Error(
                    f"units[{name!r}] must be a non-negative integer, not {count!r}")
            checked[name] = count
        record["units"] = checked
    for field in ("maxUsd", "providerCostUsd"):
        if field in record:
            value = record.get(field)
            if value is None and field in nullable_money:
                continue
            if not is_money_string(value):
                raise L2Error(f"{field} must be a money string, not {value!r}")
    if "reason" in record and not isinstance(record.get("reason"), str):
        raise L2Error(f"reason must be a string, not {record.get('reason')!r}")
    return record


def _answer(answer: Any, request: Any, *text_fields: str) -> dict[str, Any]:
    """Refuse an answer that is not an object naming the request's operation.

    A request that cannot be trusted — not an object, or without a 1-512
    character `operationId` — is `L2Error` before any key is read from it.
    """
    if not isinstance(request, dict):
        raise L2Error(f"the L2 request must be an object, not {type(request).__name__}")
    operation = request.get("operationId")
    if not is_identity(operation):
        raise L2Error("the L2 request must carry a 1-512 character operationId")
    if not isinstance(answer, dict):
        raise L2Error(f"the L2 answer must be an object, not {type(answer).__name__}")
    if answer.get("operationId") != operation:
        raise L2Error(f"answer names {answer.get('operationId')!r}, not {operation!r}")
    if "attemptId" in request:
        attempt = request.get("attemptId")
        if not is_identity(attempt):
            raise L2Error("the L2 request attemptId must be a 1-512 character string")
        if answer.get("attemptId") != attempt:
            raise L2Error(f"answer names {answer.get('attemptId')!r}, not {attempt!r}")
    for field in text_fields:
        if not isinstance(answer.get(field), str) or not answer.get(field):
            raise L2Error(f"{field} must be a non-empty string")
    return answer


def quote_request(*, operationId: str, accountId: str, product: str,
                  units: dict[str, int], priceVersion: str) -> dict[str, Any]:
    """A quote request: identity, units and a price version, guarded by `_check`."""
    return _check({"operationId": operationId, "accountId": accountId, "product": product,
                   "units": units, "priceVersion": priceVersion})


def reserve_request(*, operationId: str, attemptId: str, accountId: str, product: str,
                    units: dict[str, int], maxUsd: str,
                    priceVersion: str) -> dict[str, Any]:
    """A reserve request; `maxUsd` is money and the whole record is guarded."""
    return _check({"operationId": operationId, "attemptId": attemptId,
                   "accountId": accountId, "product": product, "units": units,
                   "maxUsd": maxUsd, "priceVersion": priceVersion})


def settle_request(*, operationId: str, attemptId: str, units: dict[str, int],
                   providerCostUsd: str | None | _Absent = PROVIDER_COST_ABSENT,
                   ) -> dict[str, Any]:
    """A settle request; the whole record is guarded.

    `providerCostUsd=None` states an explicit `null` — the covered answer —
    while the `PROVIDER_COST_ABSENT` sentinel leaves the key out. Any other
    non-money value is refused with `L2Error`. This is the same mapping the
    answer side uses: null is stated, absence is an absent key.
    """
    record: dict[str, Any] = {"operationId": operationId, "attemptId": attemptId,
                              "units": units}
    if providerCostUsd is not PROVIDER_COST_ABSENT:
        record["providerCostUsd"] = providerCostUsd
    return _check(record, nullable_money=("providerCostUsd",))


def release_request(*, operationId: str, attemptId: str,
                    reason: str | None = None) -> dict[str, Any]:
    """A release request; `reason`, when present, must be a string."""
    record: dict[str, Any] = {"operationId": operationId, "attemptId": attemptId}
    if reason is not None:
        record["reason"] = reason
    return _check(record)


def coverage_request(*, operationId: str, attemptId: str, accountId: str,
                     product: str) -> dict[str, Any]:
    """A coverage request: identity only, guarded by `_check`."""
    return _check({"operationId": operationId, "attemptId": attemptId,
                   "accountId": accountId, "product": product})


def parse_quote(answer: Any, request: dict[str, Any]) -> dict[str, Any]:
    """A price version and a non-negative money string."""
    _answer(answer, request, "priceVersion")
    if not is_money_string(answer.get("requiredUsd")):
        raise L2Error(f"requiredUsd must be a money string, not {answer.get('requiredUsd')!r}")
    return answer


def parse_reserve(answer: Any, request: dict[str, Any]) -> dict[str, Any]:
    """The reservation id, price version and held money."""
    _answer(answer, request, "reservationId", "priceVersion")
    if not is_money_string(answer.get("heldUsd")):
        raise L2Error(f"heldUsd must be a money string, not {answer.get('heldUsd')!r}")
    return answer


def parse_settle(answer: Any, request: dict[str, Any]) -> dict[str, Any]:
    """The receipt id and confirmed debit.

    The frozen schema requires `customerDebitUsd`: an explicit `null` means
    covered, but a missing key is not an answer and is refused. `None` states
    null here exactly as `settle_request(providerCostUsd=None)` states it on
    the request side; absence is a distinct spelling on both halves.
    """
    _answer(answer, request, "receiptId")
    if "customerDebitUsd" not in answer:
        raise L2Error("customerDebitUsd is absent; null means covered, absence is not an answer")
    debit = answer.get("customerDebitUsd")
    if debit is not None and not is_money_string(debit):
        raise L2Error(f"customerDebitUsd must be a money string or null, not {debit!r}")
    return answer


def parse_release(answer: Any, request: dict[str, Any]) -> dict[str, Any]:
    """`released` must be exactly True; `reason` is optional."""
    _answer(answer, request)
    if answer.get("released") is not True:
        raise L2Error(f"a release answer says released=True, not {answer.get('released')!r}")
    reason = answer.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise L2Error(f"release reason must be a string or null, not {reason!r}")
    return answer


def parse_coverage(answer: Any, request: dict[str, Any]) -> dict[str, Any] | None:
    """The launch-coverage block, or None when there is no coverage."""
    if answer is None:
        return None
    _answer(answer, request)
    try:
        return validate_coverage(answer)
    except ReceiptError as exc:
        raise L2Error(str(exc)) from exc
