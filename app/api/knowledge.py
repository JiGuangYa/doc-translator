"""Local glossary and exact-match translation memory APIs."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import auth, store
from ..services import memory

router = APIRouter()


class GlossaryEntry(BaseModel):
    source: str
    target: str


class GlossaryBody(BaseModel):
    entries: list[GlossaryEntry]


@router.get("/api/glossary")
def get_glossary():
    return {"entries": store.load_glossary()}


@router.put("/api/glossary")
def put_glossary(body: GlossaryBody, request: Request):
    entries = store.save_glossary([entry.model_dump() for entry in body.entries])
    auth.audit("glossary_update", count=len(entries),
               ip=request.client.host if request.client else "")
    return {"entries": entries}


@router.get("/api/memory")
def get_memory():
    return {"count": memory.count(), "entries": memory.list_entries()}


@router.delete("/api/memory")
def clear_memory(request: Request):
    memory.clear()
    auth.audit("translation_memory_clear",
               ip=request.client.host if request.client else "")
    return {"ok": True}
