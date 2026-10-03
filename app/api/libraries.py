"""Explicit local library import. Source libraries are read-only."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import migration

router = APIRouter()


class ImportBody(BaseModel):
    source: str
    kind: str = "auto"
    allow_keychain: bool = True
    native_reading: dict | None = None


@router.post("/api/libraries/import")
def import_library(body: ImportBody):
    try:
        return migration.import_library(body.source, body.kind, body.allow_keychain, body.native_reading)
    except (OSError, ValueError) as error:
        raise HTTPException(400, str(error)) from error
