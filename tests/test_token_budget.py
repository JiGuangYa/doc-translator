"""Per-task usage is durable and pauses work at a configured token budget."""

from docx import Document

from app import config, store
from app.api.tasks import _summary
from app.services import pipeline, task_manager
from app.services.llm import translator


def test_budget_pauses_and_keeps_completed_translation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    task_manager.reset_shutdown()
    task_id = "d" * 32
    source = tmp_path / "source.docx"
    document = Document()
    document.add_paragraph("A full sentence to translate")
    document.save(source)
    job = pipeline.parse_upload(task_id, source.name, source, {})
    job.update(status="translating", provider_id="fake", source_lang="en",
               target_lang="zh-CN", token_budget=10,
               input_price_per_million=1.0, output_price_per_million=2.0)
    store.save_job(task_id, job)

    def translate(segments, _provider, _source, _target, _settings,
                  existing=None, progress_cb=None, cancel_check=None, usage_cb=None, **kwargs):
        usage_cb(6, 5)
        assert cancel_check()
        raise translator.TranslationCancelled(partial={segments[0].seg_id: "完整译文"})

    monkeypatch.setattr(translator, "translate_segments", translate)
    pipeline._run_translation(task_id, ".docx", "fake", "en", "zh-CN", store.load_settings())
    done = store.load_job(task_id)
    assert done["status"] == "paused"
    assert done["usage"] == {"prompt_tokens": 6, "completion_tokens": 5}
    assert (store.task_dir(task_id) / "translated.docx").exists()
    assert _summary(done)["estimated_cost_usd"] == 0.000016
