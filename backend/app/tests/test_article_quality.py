from app.services.task_result_processors import article_quality_errors, render_article_markdown


def test_article_body_contract_is_plain_text() -> None:
    article = {
        "title": "Как команда принимает управленческие решения",
        "subtitle": "Практический взгляд",
        "lead": "Руководители часто видят одну проблему по-разному.",
        "sections": [
            {"key": "situation", "heading": "Ситуация", "body_markdown": "Описание ситуации."},
            {"key": "decision", "heading": "Решение", "body_markdown": "Практический разбор."},
            {"key": "action", "heading": "Действие", "body_markdown": "Следующий шаг команды."},
        ],
        "conclusion": "Решение становится рабочим, когда у него есть общий смысл и ответственный.",
        "cta": "Сопоставьте этот вопрос с ситуацией вашей команды.",
    }
    rendered = render_article_markdown(article)
    assert not rendered.startswith("#")
    assert "## " not in rendered
    assert article_quality_errors(rendered) == []


def test_article_quality_rejects_format_and_internal_artifacts() -> None:
    text = "x" * 120 + " **служебный** CTA: section_key provenance"
    errors = article_quality_errors(text)
    assert "markdown_bold" in errors
    assert "internal_label:section_key" in errors
    assert "internal_label:provenance" in errors


def test_article_quality_accepts_sufficient_plain_text() -> None:
    assert article_quality_errors("Текст статьи. " * 20) == []
