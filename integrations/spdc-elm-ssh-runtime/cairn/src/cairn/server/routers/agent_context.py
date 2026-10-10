from __future__ import annotations

from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import AgentContextTemplate, AgentContextTemplateCreate, AgentContextTemplateUpdate
from cairn.server.services import (
    create_agent_context_template,
    delete_agent_context_template,
    get_agent_context_template_or_404,
    list_agent_context_templates,
    update_agent_context_template,
)

router = APIRouter(prefix="/agent-context", tags=["agent-context"])


@router.get("/templates", response_model=list[AgentContextTemplate])
def list_templates():
    with get_conn() as conn:
        return list_agent_context_templates(conn)


@router.post("/templates", response_model=AgentContextTemplate, status_code=201)
def create_template(body: AgentContextTemplateCreate):
    with get_conn() as conn:
        return create_agent_context_template(conn, body)


@router.get("/templates/{template_id}", response_model=AgentContextTemplate)
def get_template(template_id: str):
    with get_conn() as conn:
        return get_agent_context_template_or_404(conn, template_id)


@router.patch("/templates/{template_id}", response_model=AgentContextTemplate)
def update_template(template_id: str, body: AgentContextTemplateUpdate):
    with get_conn() as conn:
        return update_agent_context_template(conn, template_id, body)


@router.delete("/templates/{template_id}", status_code=204)
def delete_template(template_id: str):
    with get_conn() as conn:
        delete_agent_context_template(conn, template_id)
