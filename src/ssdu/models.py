from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from ssdu.dataset_contract import Dataset


class RetrievalError(RuntimeError):
    """A source request cannot produce a complete, trustworthy product."""


@dataclass(frozen=True)
class Request:
    provider: str
    dataset_id: str
    params: dict[str, Any] = field(default_factory=dict)
    page_size: int = 1000
    max_rows: int = 10000

    def __post_init__(self):
        if not self.provider or not self.dataset_id:
            raise ValueError("provider and dataset_id are required")
        if not 1 <= self.page_size <= 10000:
            raise ValueError("page_size must be between 1 and 10000")
        if not 1 <= self.max_rows <= 100000:
            raise ValueError("max_rows must be between 1 and 100000 for this local PoC")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Acquisition:
    provider: str
    dataset_id: str
    title: str
    frame: pd.DataFrame
    metadata: dict[str, Any]
    release: dict[str, Any]
    licence: str
    citation: str
    source_url: str
    dataset: Dataset | None = None
