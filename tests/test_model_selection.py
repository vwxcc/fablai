from app.llm import resolve_generation_model, resolve_request_model


def test_admin_model_overrides_render_default():
    assert resolve_generation_model("admin/model", "render/model") == "admin/model"


def test_render_default_is_used_without_admin_override():
    assert resolve_generation_model("", "render/model") == "render/model"


def test_provider_model_is_used_when_no_global_model_is_configured():
    assert resolve_generation_model("", "") is None
    assert resolve_request_model("provider/model", None) == "provider/model"


def test_global_model_overrides_each_generation_provider_model():
    assert resolve_request_model("provider-a/model", "global/model") == "global/model"
    assert resolve_request_model("provider-b/model", "global/model") == "global/model"
