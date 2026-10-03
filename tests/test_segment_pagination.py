"""Large-document clients can page segments without changing the old API default."""

from app import config, store


def test_paginated_segments_keep_legacy_full_response(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    task_id = "f" * 32
    store.save_job(task_id, {
        "task_id": task_id, "filename": "sample.docx", "ext": ".docx",
        "status": "done", "segments": [
            {"seg_id": f"s{n:06d}", "text": f"Sentence {n}", "translation": f"译文 {n}",
             "context": "", "meta": {}, "translatable": True}
            for n in range(3)],
        "translations": {}, "warnings": [],
    })
    all_segments = client.get(f"/api/tasks/{task_id}/segments").json()
    assert len(all_segments["segments"]) == 3
    first = client.get(f"/api/tasks/{task_id}/segments?offset=0&limit=2").json()
    second = client.get(f"/api/tasks/{task_id}/segments?offset=2&limit=2").json()
    assert first["total"] == second["total"] == 3
    assert [segment["seg_id"] for segment in first["segments"] + second["segments"]] == [
        "s000000", "s000001", "s000002"]


def test_search_filters_entire_document_before_pagination(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    task_id = "b" * 32
    segments = [{"seg_id": f"s{n:06d}", "text": f"Source {n}", "context": f"Sheet!A{n + 1}",
                 "translation": None, "translatable": True, "meta": {}}
                for n in range(10000)]
    segments[9001]["text"] = "A+B & Straße 中文"
    segments[9900]["translatable"] = False
    store.save_job(task_id, {"task_id": task_id, "filename": "large.xlsx", "ext": ".xlsx", "segments": segments,
                           "translations": {"s009999": "目标：最后一行"}, "revision": 7})
    url = f"/api/tasks/{task_id}/segments"
    response = client.get(url, params={"limit": 100})
    assert len(response.json()["segments"]) == 100
    assert response.json()["total"] == 10000
    assert len(response.content) < 40000
    found = client.get(url, params={"q": "a+b & STRASSE 中文", "limit": 100}).json()
    assert [s["seg_id"] for s in found["segments"]] == ["s009001"]
    assert found["revision"] == 7
    by_translation = client.get(url, params={"q": "最后一行", "limit": 100}).json()
    assert by_translation["total"] == 1
    assert by_translation["segments"][0]["translation"] == "目标：最后一行"
    assert client.get(url, params={"q": "最后一行", "filter": "untranslated", "limit": 100}).json()["total"] == 0
    assert client.get(url, params={"q": "Sheet!A9999", "limit": 100}).json()["segments"][0]["seg_id"] == "s009998"
    assert client.get(url, params={"filter": "untranslated", "offset": 9990, "limit": 100}).json()["total"] == 9998


def test_worksheet_list_is_complete_with_filters_and_bang_in_name(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    task_id = "c" * 32
    store.save_job(task_id, {"task_id": task_id, "filename": "sheets.xlsx", "ext": ".xlsx", "segments": [
        {"seg_id": str(n), "text": f"Text {n}", "context": f"{sheet}!A1", "translatable": True}
        for n, sheet in enumerate(["First", "Sales!2026", "最后工作表"])]})
    response = client.get(f"/api/tasks/{task_id}/segments", params={"sheet": "Sales!2026", "limit": 1}).json()
    assert response["total"] == 1
    assert response["segments"][0]["context"] == "Sales!2026!A1"
    assert response["sheets"] == ["First", "Sales!2026", "最后工作表"]


def test_ocr_review_filter_covers_all_pages_and_excludes_confirmed(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    task_id = "d" * 32
    metadata = [{"confidence": None}, {}, {"confidence": 0.9}, {"confidence": 0.4},
                {"needs_review": True, "confidence": 0.99}, {"confidence": 0.4, "reviewed": True}]
    store.save_job(task_id, {"task_id": task_id, "filename": "review.pdf", "ext": ".pdf", "segments": [
        {"seg_id": str(n), "text": "Review text", "context": f"Page {n + 1}", "translatable": True, "meta": meta}
        for n, meta in enumerate(metadata)]})
    response = client.get(f"/api/tasks/{task_id}/segments", params={"review_only": True, "offset": 1, "limit": 1}).json()
    assert response["total"] == 2
    assert response["segments"][0]["seg_id"] == "4"


def test_draft_search_anchor_and_layout_filter_are_paginated(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TASKS_DIR', tmp_path / 'tasks')
    identifier = 'e' * 32
    store.save_job(identifier, {'task_id': identifier, 'ext': '.docx', 'filename': 'Long.docx',
        'segments': [{'seg_id': f's{i:06d}', 'text': f'Paragraph {i}', 'translatable': True} for i in range(10000)],
        'translations': {}, 'revision': 3, 'overflow': ['s008123']})
    store._atomic_write_json(config.DATA_DIR / 'revision-drafts.json', {'schema_version': 2, 'tasks': {
        identifier: {'revision': 3, 'segments': {'s009999': 'Draft-only keyword', 's000001': ''}}}})
    route = f'/api/tasks/{identifier}/segments'
    response = client.get(route, params={'q': 'draft-only', 'limit': 100}).json()
    assert response['total'] == 1 and response['segments'][0]['seg_id'] == 's009999'
    response = client.get(route, params={'filter': 'drafts', 'limit': 100}).json()
    assert response['total'] == 2
    response = client.get(route, params={'anchor_id': 's008123', 'limit': 100}).json()
    assert response['offset'] == 8100 and len(response['segments']) == 100
    response = client.get(route, params={'filter': 'layout', 'limit': 100}).json()
    assert [s['seg_id'] for s in response['segments']] == ['s008123']
