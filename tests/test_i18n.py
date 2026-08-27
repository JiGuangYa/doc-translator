"""Test the i18n catalog system and the /api/i18n endpoint."""
from app import i18n


def test_t_falls_back_to_default():
    """Missing keys return {key} so the UI shows a developer identifier."""
    out = i18n.t("definitely.not.a.key")
    assert out == "{definitely.not.a.key}"


def test_t_english_default():
    assert i18n.t("common.save", "en") == "Save"


def test_t_zh_cn():
    assert i18n.t("common.save", "zh-CN") == "保存"


def test_t_family_fallback():
    """If we ship 'zh-CN' but ask for 'zh-TW', we fall back to 'zh' family."""
    # We don't ship zh-TW, so resolution should pick the closest family
    lang = i18n.resolve_lang("zh-TW")
    assert lang in ("zh-CN", "zh")  # family match OR default if no family match


def test_available_languages_includes_en_and_zh():
    langs = i18n.available_languages()
    codes = [l["code"] for l in langs]
    assert "en" in codes
    assert "zh-CN" in codes
    for l in langs:
        assert "code" in l and "name" in l


def test_api_i18n_endpoint(anon_client):
    """GET /api/i18n/<lang>.json returns the full catalog (anonymous)."""
    r = anon_client.get("/api/i18n/en.json")
    assert r.status_code == 200
    cat = r.json()
    assert cat["_name"] == "English"
    assert cat["common.save"] == "Save"


def test_api_i18n_unknown_lang_returns_english(anon_client):
    r = anon_client.get("/api/i18n/xx-YY.json")
    assert r.status_code == 200
    cat = r.json()
    # Falls back to the English catalog
    assert cat["_name"] == "English"


def test_api_i18n_list(anon_client):
    """GET /api/i18n returns the list of shipped languages."""
    r = anon_client.get("/api/i18n")
    assert r.status_code == 200
    body = r.json()
    codes = [l["code"] for l in body["languages"]]
    assert "en" in codes
    assert "zh-CN" in codes
