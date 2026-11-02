"""Chunking, JSON parsing, terms and the glossary: the pure-Python parts."""
import pytest

from translator.engine import make_pages, parse_json_object, split_long
from translator.glossary import Glossary, _norm, extract_terms, fix_lookalikes, stem


# ------------------------------------------------------------------ chunking

def test_pages_keep_order_and_respect_size():
    paragraphs = [f"Абзац номер {i} " + "текст " * 20 for i in range(50)]
    pages = make_pages(paragraphs, page_chars=500)
    assert [p.paragraph for page in pages for p in page] == paragraphs  # nothing lost, nothing reordered
    assert all(sum(len(p.text) for p in page) <= 500 for page in pages if len(page) > 1)


def test_long_paragraph_is_split_at_sentences():
    text = " ".join(f"Предложение номер {i}." for i in range(100))
    parts = split_long(text, 200)
    assert all(len(p) <= 200 for p in parts)
    assert " ".join(parts) == text


def test_sentence_longer_than_a_page_is_split_at_spaces():
    parts = split_long("слово " * 100, 50)
    assert all(len(p) <= 50 for p in parts)


# ------------------------------------------------------------------ JSON from models

@pytest.mark.parametrize("answer", [
    '{"1": "сәлем"}',
    '```json\n{"1": "сәлем"}\n```',
    'Here is the translation: {"1": "сәлем"} Hope this helps! {"2": "x"}',
])
def test_parse_json_object_tolerates_wrapping(answer):
    assert parse_json_object(answer) == {"1": "сәлем"}


@pytest.mark.parametrize("answer", ["", "no json here", "[1, 2]"])
def test_parse_json_object_rejects_garbage(answer):
    with pytest.raises(ValueError):
        parse_json_object(answer)


# ------------------------------------------------------------------ terms

def test_stem_groups_case_forms():
    assert stem("договор") == stem("договора") == stem("договору")


def test_extract_terms_counts_forms_together_and_skips_verbs():
    paragraphs = ["Договор поставки подписан.", "Условия договора поставки.", "По договору поставки платить.",
                  "Договор поставки действует."] * 2
    terms = extract_terms(paragraphs, max_terms=5, min_count=3)
    assert "договор поставки" in terms
    assert not any(t.endswith("ть") for t in terms)


def test_vote_normalization_plural_and_lookalikes():
    assert _norm("ұпайлар") == _norm("ұпай")
    assert _norm("ережe") == _norm("ереже")  # Latin e
    assert fix_lookalikes("PNG ережe") == "PNG ереже"


def test_glossary_matches_equal_stems_only():
    g = Glossary({"поля": "өрістер", "договор поставки": "жеткізу шарты"})
    assert g.for_text("Заполните поля формы") == [("поля", "өрістер")]
    assert g.for_text("Пользователь вошёл") == []  # 'пол' must not match 'пользователь'
    assert ("договор поставки", "жеткізу шарты") in g.for_text("Условия договора поставки")
