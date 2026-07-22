import uuid

import pytest
from pydantic import ValidationError

from vectorless_rag.models import RunRoute
from vectorless_rag.schemas import ChatRequest, Citation, RouteDecision


def test_chat_request_normalizes_whitespace() -> None:
    assert ChatRequest(message="  question  ").message == "question"
    with pytest.raises(ValidationError):
        ChatRequest(message="   ")


def test_document_citation_requires_resolvable_pages() -> None:
    with pytest.raises(ValidationError):
        Citation(source_type="document", source_id="id", title="Paper")
    citation = Citation(
        source_type="document", source_id="id", title="Paper", page_start=2, page_end=3
    )
    assert citation.page_start == 2


def test_sql_citation_requires_audit_id() -> None:
    with pytest.raises(ValidationError):
        Citation(source_type="sql", source_id="id", title="Catalog")
    audit_id = uuid.uuid4()
    citation = Citation(
        source_type="sql", source_id=str(audit_id), title="Catalog", sql_audit_id=audit_id
    )
    assert citation.sql_audit_id == audit_id


def test_route_schema_is_closed() -> None:
    assert (
        RouteDecision(
            route=RunRoute.hybrid,
            reason="needs filtering",
            standalone_question="Compare the selected papers",
        ).route
        == RunRoute.hybrid
    )
    assert (
        RouteDecision(
            route=RunRoute.help,
            reason="product capabilities",
            standalone_question="What can you do?",
        ).route
        == RunRoute.help
    )
    with pytest.raises(ValidationError):
        RouteDecision.model_validate(
            {
                "route": "web",
                "reason": "unsupported",
                "standalone_question": "Search the web",
            }
        )
