from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


SettlementStatus = Literal["settled", "pending", "rejected"]


@dataclass(frozen=True, slots=True)
class SettlementOutcome:
    status: SettlementStatus
    transaction: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status == "settled" and not valid_settlement_reference(self.transaction):
            raise ValueError("settled outcome requires transaction")
        if self.status == "pending" and not valid_settlement_reference(self.transaction):
            raise ValueError("pending outcome requires transaction")
        if self.status == "rejected" and self.transaction is not None:
            raise ValueError("rejected outcome cannot contain transaction")


def valid_settlement_reference(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 4096
        and all("!" <= character <= "~" for character in value)
    )
