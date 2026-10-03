"""Local terminology and reviewed translations are reusable without a model call."""

from app import config, store
from app.services import memory
from app.services.llm.prompts import build_user_prompt


def test_reviewed_memory_wins_and_can_be_cleared(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    memory.remember("en", "zh-CN", "Engine", "provider-a", "引擎")
    assert memory.lookup("en", "zh-CN", "Engine", "provider-a") == "引擎"
    assert memory.lookup("en", "zh-CN", "Engine", "provider-b") is None
    memory.remember("en", "zh-CN", "Engine", "provider-a", "发动机", verified=True)
    memory.remember("en", "zh-CN", "Engine", "provider-a", "引擎", verified=False)
    assert memory.lookup("en", "zh-CN", "Engine", "provider-b") == "发动机"
    assert memory.count() == 1
    memory.clear()
    assert memory.count() == 0


def test_glossary_only_adds_relevant_terms(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GLOSSARY_FILE", tmp_path / "glossary.json")
    saved = store.save_glossary([
        {"source": "engine", "target": "引擎"},
        {"source": "harbor", "target": "港口"},
    ])
    prompt = build_user_prompt('{"s000001":"The engine starts"}', "en", "zh-CN",
                               glossary=saved)
    assert '"target": "引擎"' in prompt
    assert '"target": "港口"' not in prompt
    assert len(store.load_glossary()) == 2
