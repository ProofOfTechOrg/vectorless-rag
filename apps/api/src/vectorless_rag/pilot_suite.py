from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictPilotModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PilotDocument(StrictPilotModel):
    arxiv_id: str = Field(pattern=r"^\d{4}\.\d{4,5}$")
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=1)
    pdf_outline: bool

    @property
    def page_band(self) -> str:
        if self.page_count <= 14:
            return "le_14"
        if self.page_count <= 19:
            return "15_19"
        if self.page_count <= 26:
            return "20_26"
        if self.page_count <= 37:
            return "27_37"
        return "gt_37"


class PilotManifest(StrictPilotModel):
    schema_version: Literal[1]
    name: Literal["pageindex-v2-25"]
    corpus_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    documents: list[PilotDocument] = Field(min_length=25, max_length=25)

    @model_validator(mode="after")
    def representative_shape(self) -> PilotManifest:
        identifiers = [document.arxiv_id for document in self.documents]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("pilot manifest has duplicate arXiv IDs")
        digests = [document.pdf_sha256 for document in self.documents]
        if len(set(digests)) != len(digests):
            raise ValueError("pilot manifest has duplicate PDF digests")
        bands = Counter(document.page_band for document in self.documents)
        expected = {"le_14": 8, "15_19": 4, "20_26": 4, "27_37": 4, "gt_37": 5}
        if bands != expected:
            raise ValueError(f"pilot page bands do not match the frozen design: {dict(bands)}")
        if not any(document.pdf_outline for document in self.documents) or not any(
            not document.pdf_outline for document in self.documents
        ):
            raise ValueError("pilot requires PDFs with and without outlines")
        return self


def load_pilot_manifest(path: Path) -> PilotManifest:
    return PilotManifest.model_validate_json(path.read_text(encoding="utf-8"))
