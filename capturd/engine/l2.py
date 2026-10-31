"""The five L2 calls as data: request records with identity, response parsers
that refuse a record naming another operation. The injected client is the
control plane's `l2-client.mjs` at `rhobear-builds`; no money arithmetic.
"""

from __future__ import annotations

from typing import Any

from .receipts import ReceiptError, is_money_string, validate_coverage


class L2Error(ValueError):
    """The client answered with a record that is not the call's shape."""


def _answer(answer: Any, request: dict[str, Any], *text_fields: str) -> dict[str, Any]:
    if not isinstance(answer, dict):
        raise L2Error(f"the L2 answer must be an object, not {type(answer).__name__}")
    if answer.get("operationId") != request["operationId"]:
        raise L2Error(f"answer names {answer.get('operationId')!r}, not {request['operationId']!r}")
    if "attemptId" in request and answer.get("attemptId") != request["attemptId"]:
        raise L2Error(f"answer names {answer.get('attemptId')!r}, not {request['attemptId']!r}")
    for field in text_fields:
        if not isinstance(answer.get(field), str) or not answer.get(field):
            raise L2Error(f"{field} must be a non-empty string")
    return answer


def quote_request(*, operationId: str, accountId: str, product: str,
                  units: dict[str, int], priceVersion: str) -> dict[str, Any]:
    return {"operationId": operationId, "accountId": accountId, "product": product,
            "units": dict(units), "priceVersion": priceVersion}


def reserve_request(*, operationId: str, attemptId: str, accountId: str, product: str,
                    units: dict[str, int], maxUsd: str,
                    priceVersion: str) -> dict[str, Any]:
    if not is_money_string(maxUsd):
        raise L2Error(f"maxUsd must be a money string, not {maxUsd!r}")
    return {"operationId": operationId, "attemptId": attemptId, "accountId": accountId,
            "product": product, "units": dict(units), "maxUsd": maxUsd,
            "priceVersion": priceVersion}


def settle_request(*, operationId: str, attemptId: str, units: dict[str, int],
                   providerCostUsd: str | None = None) -> dict[str, Any]:
    if providerCostUsd is not None and not is_money_string(providerCostUsd):
        raise L2Error(f"providerCostUsd must be a money string or null, not {providerCostUsd!r}")
    return {"operationId": operationId, "attemptId": attemptId, "units": dict(units),
            **({"providerCostUsd": providerCostUsd} if providerCostUsd is not None else {})}


def release_request(*, operationId: str, attemptId: str,
                    reason: str | None = None) -> dict[str, Any]:
    return {"operationId": operationId, "attemptId": attemptId,
            **({"reason": reason} if reason is not None else {})}


def coverage_request(*, operationId: str, attemptId: str, accountId: str,
                     product: str) -> dict[str, Any]:
    return {"operationId": operationId, "attemptId": attemptId, "accountId": accountId,
            "product": product}


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
    covered, but a missing key is not an answer and is refused. Absence and
    null are different answers.
    """
    _answer(answer, request, "receiptId")
    if "customerDebitUsd" not in answer:
        raise L2Error("customerDebitUsd is absent; null means covered, absence is not an answer")
    debit = answer["customerDebitUsd"]
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
