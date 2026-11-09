from app.rendering import split_telegram_html, to_telegram_html


def test_markdown_is_rendered_as_telegram_html():
    result = to_telegram_html("**Заголовок**\n\nОбычный *текст*.")
    assert "<b>Заголовок</b>" in result
    assert "<i>текст</i>" in result


def test_markdown_table_uses_preformatted_text_not_html_table():
    result = to_telegram_html("| Модель | Баллы |\n|---|---:|\n| A | 10 |")
    assert "<pre>" in result
    assert "<table" not in result
    assert "Модель" in result
    assert "Баллы" in result


def test_unsafe_html_and_links_are_removed():
    result = to_telegram_html(
        '<b>Ок</b><script>alert("bad")</script>'
        '<a href="javascript:alert(1)">опасная ссылка</a>'
    )
    assert "<b>Ок</b>" in result
    assert "alert" not in result
    assert 'href="javascript:' not in result
    assert "опасная ссылка" in result


def test_split_preserves_balanced_formatting_tags():
    content = "<b>" + ("текст " * 300) + "</b>"
    parts = split_telegram_html(content, limit=512)
    assert len(parts) > 1
    assert all(part.startswith("<b>") for part in parts)
    assert all(part.endswith("</b>") for part in parts)
    assert all(len(part) < 600 for part in parts)
