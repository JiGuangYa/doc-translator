"""Provider CRUD, connectivity test, global settings."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import auth, store
from ..services.llm import client
from ..services import ssrf_guard

router = APIRouter()


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


class ProviderBody(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    model: str
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None


@router.get("/api/providers")
def list_providers():
    settings = store.load_settings()
    return {"providers": store.list_providers(), "settings": settings}


class ProviderTestBody(BaseModel):
    provider_id: str | None = None
    base_url: str
    api_key: str = ""
    model: str


class ModelListBody(BaseModel):
    provider_id: str | None = None
    base_url: str
    api_key: str = ""


@router.post("/api/providers/models")
def list_unsaved_models(body: ModelListBody):
    if not body.base_url.strip():
        raise HTTPException(400, "base_url is required")
    return client.list_models(body.base_url.strip(), body.api_key or (store.get_provider_secret(body.provider_id) if body.provider_id else ""))


@router.get("/api/providers/{provider_id}/models")
def list_saved_models(provider_id: str):
    provider = store.get_provider(provider_id)
    if not provider:
        raise HTTPException(404, "provider not found")
    return client.list_models(provider["base_url"], store.get_provider_secret(provider_id))


@router.post("/api/providers/test")
def test_unsaved_provider(body: ProviderTestBody):
    if not body.base_url.strip() or not body.model.strip():
        raise HTTPException(400, "base_url and Model are required")
    return client.test_connection(body.base_url.strip(), body.api_key or (store.get_provider_secret(body.provider_id) if body.provider_id else ""), body.model.strip())


@router.post("/api/providers")
def create_provider(body: ProviderBody, request: Request):
    if not body.name.strip() or not body.base_url.strip() or not body.model.strip():
        raise HTTPException(400, "Name / base_url / Model are required")
    if any(value is not None and value < 0 for value in
           (body.input_price_per_million, body.output_price_per_million)):
        raise HTTPException(400, "Token prices cannot be negative")
    base_url = client.normalize_chat_base_url(body.base_url)
    ok, reason = ssrf_guard.check_base_url(base_url)
    if not ok:
        raise HTTPException(400, f"base_url rejected by SSRF guard: {reason}")
    payload = body.model_dump()
    payload["base_url"] = base_url
    rec = store.save_provider(payload)
    auth.audit("provider_create", provider_id=rec["id"], name=body.name,
               base_url=base_url, ip=_ip(request))
    return {"ok": True, "provider": rec}


@router.put("/api/providers/{provider_id}")
def update_provider(provider_id: str, body: ProviderBody, request: Request):
    if any(value is not None and value < 0 for value in
           (body.input_price_per_million, body.output_price_per_million)):
        raise HTTPException(400, "Token prices cannot be negative")
    base_url = client.normalize_chat_base_url(body.base_url)
    if base_url:
        ok, reason = ssrf_guard.check_base_url(base_url)
        if not ok:
            raise HTTPException(400, f"base_url rejected by SSRF guard: {reason}")
    try:
        payload = body.model_dump()
        payload["base_url"] = base_url
        rec = store.save_provider(payload, provider_id=provider_id)
    except KeyError:
        raise HTTPException(404, "provider not found") from None
    auth.audit("provider_update", provider_id=provider_id, name=body.name,
               base_url=base_url, ip=_ip(request))
    return {"ok": True, "provider": rec}


@router.delete("/api/providers/{provider_id}")
def delete_provider(provider_id: str, request: Request):
    ok = store.delete_provider(provider_id)
    if ok:
        auth.audit("provider_delete", provider_id=provider_id, ip=_ip(request))
    return {"ok": ok}


@router.post("/api/providers/{provider_id}/test")
def test_provider(provider_id: str):
    result = client.test_provider(provider_id)
    return result


class SettingsBody(BaseModel):
    translation_provider_id: str | None = None
    assistant_provider_id: str | None = None
    source_lang: str | None = None
    target_lang: str | None = None
    translate_notes: bool | None = None
    batch_max_chars: int | None = None
    batch_max_segments: int | None = None
    concurrency_batches: int | None = None
    task_retention_days: int | None = None
    use_translation_memory: bool | None = None


@router.put("/api/settings")
def update_settings(body: SettingsBody, request: Request):
    data = body.model_dump(exclude_unset=True)
    # Clamp to a safe range: an oversized batch can make a single LLM response truncate,
    # losing the whole batch.
    if data.get("batch_max_chars") is not None:
        data["batch_max_chars"] = max(200, min(20000, data["batch_max_chars"]))
    if data.get("batch_max_segments") is not None:
        data["batch_max_segments"] = max(1, min(100, data["batch_max_segments"]))
    if data.get("concurrency_batches") is not None:
        data["concurrency_batches"] = max(1, min(10, data["concurrency_batches"]))
    updated = store.save_settings(data)
    changed = {k: v for k, v in data.items() if v is not None}
    auth.audit("settings_update", keys=sorted(changed), ip=_ip(request))
    return {"ok": True, "settings": updated}
