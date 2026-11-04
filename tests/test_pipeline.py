"""The whole pipeline end to end with fake models: read -> pages -> parallel translation with
failover -> reassembly in order -> memory -> glossary. Runs in about a second, costs nothing."""
import pytest
from docx import Document

from tezau.docx_io import iter_paragraphs, open_document
from tezau.engine import translate_document
from tezau.glossary import Glossary, extract_terms, vote_terms
from tezau.memory import Store
from tezau.providers import ProviderError

PARAGRAPHS = [f"Пункт {i}. Поставщик передает товар покупателю по договору поставки." for i in range(60)]


def texts(path):
    return [p.text for p in iter_paragraphs(open_document(path))]


def test_every_paragraph_translated_in_order(make_docx, make_provider, tmp_path):
    src = make_docx(PARAGRAPHS + ["2026", ""])
    out = str(tmp_path / "out.docx")
    r = translate_document(src, out, [make_provider("a"), make_provider("b")], block_pages=1, page_chars=300)
    result = texts(out)
    assert result[:60] == [f"[қаз] {p}" for p in PARAGRAPHS]
    assert result[60:] == ["2026", ""]  # numbers and empty lines are left alone
    assert r.pages > 1 and sum(r.pages_by_provider.values()) == r.pages


def test_tables_headers_and_formatting_are_kept(make_docx, make_provider, tmp_path):
    src = make_docx(["Важный текст договора"], table=[["Ячейка таблицы", "123"]],
                    header="Колонтитул документа", bold_first_word=True)
    out = str(tmp_path / "out.docx")
    translate_document(src, out, [make_provider()])
    doc = Document(out)
    assert doc.paragraphs[0].text == "[қаз] Важный текст договора"
    assert doc.paragraphs[0].runs[0].bold  # the translation takes the first run's formatting
    assert doc.tables[0].cell(0, 0).text == "[қаз] Ячейка таблицы"
    assert doc.tables[0].cell(0, 1).text == "123"
    assert doc.sections[0].header.paragraphs[0].text == "[қаз] Колонтитул документа"


def test_failed_provider_drops_out_and_others_take_over(make_docx, make_provider, tmp_path):
    src = make_docx(PARAGRAPHS)
    out = str(tmp_path / "out.docx")
    broken = make_provider("broken", fail_after=0)
    r = translate_document(src, out, [broken, make_provider("ok")], block_pages=1, page_chars=300)
    assert "broken" in r.failed and r.pages_by_provider["broken"] == 0
    assert texts(out) == [f"[қаз] {p}" for p in PARAGRAPHS]


def test_resume_after_all_providers_fail(make_docx, make_provider, tmp_path):
    """Finished pages are saved; the next run only translates what is missing."""
    src = make_docx(PARAGRAPHS)
    out = str(tmp_path / "out.docx")
    first = make_provider("a", fail_after=2, error=Exception("credit balance is too low"))
    with pytest.raises(ProviderError):
        translate_document(src, out, [first], block_pages=1, page_chars=300)
    second = make_provider("b")
    r = translate_document(src, out, [second], block_pages=1, page_chars=300)
    assert r.pages_by_provider["b"] == r.pages - 2  # the 2 saved pages were not sent again
    assert texts(out) == [f"[қаз] {p}" for p in PARAGRAPHS]


def test_bad_answers_are_translated_again(make_docx, make_provider, tmp_path):
    """A model that first answers with Russian left in the text gets a second chance."""
    russian = "После обновления любого правила система должна пересчитать график."
    seen = []

    def lazy_then_good(text):
        seen.append(text)
        return text if len(seen) == 1 else "Кез келген ереже жаңартылғаннан кейін жүйе кестені қайта есептеуі тиіс."

    out = str(tmp_path / "out.docx")
    translate_document(make_docx([russian]), out, [make_provider(translate=lazy_then_good)])
    assert texts(out) == ["Кез келген ереже жаңартылғаннан кейін жүйе кестені қайта есептеуі тиіс."]
    assert len(seen) == 2


def test_memory_makes_the_second_run_free(make_docx, make_provider, tmp_path):
    src = make_docx(PARAGRAPHS)
    store = Store(str(tmp_path / "t.db"))
    translate_document(src, str(tmp_path / "1.docx"), [make_provider()], store=store, page_chars=300)
    again = make_provider("again")
    r = translate_document(src, str(tmp_path / "2.docx"), [again], store=store, page_chars=300)
    assert r.pages == 0 and r.from_memory == 60 and again.usage.requests == 0
    assert texts(str(tmp_path / "2.docx")) == [f"[қаз] {p}" for p in PARAGRAPHS]


def test_bad_memory_entries_are_not_reused(make_docx, make_provider, tmp_path):
    russian = "После обновления любого правила система должна пересчитать график."
    store = Store(str(tmp_path / "t.db"))
    store.tm_put_many([(russian, russian, "old-run")])  # a bad answer saved by an old version
    fixer = make_provider(translate=lambda t: "Жүйе кестені қайта есептеуі тиіс.")
    r = translate_document(make_docx([russian]), str(tmp_path / "out.docx"), [fixer], store=store)
    assert r.from_memory == 0 and fixer.usage.requests == 1


def test_voting_and_glossary_reach_the_prompt(make_docx, make_provider, tmp_path):
    store = Store(str(tmp_path / "t.db"))
    terms = extract_terms(PARAGRAPHS, max_terms=5)
    answers = {t: "жеткізу шарты" for t in terms}
    decisions = vote_terms(terms, [make_provider("a", terms=answers), make_provider("b", terms=answers),
                                   make_provider("c", terms={t: "басқа" for t in terms})], store=store)
    assert all(d["status"] == "agreed" and d["target"] == "жеткізу шарты" for d in decisions)

    prompts = []
    p = make_provider()
    original = p._call
    p._call = lambda system, user, json_mode: (prompts.append(user), original(system, user, json_mode))[1]
    translate_document(make_docx(PARAGRAPHS[:3]), str(tmp_path / "out.docx"), [p],
                       glossary=Glossary.from_store(store))
    assert "жеткізу шарты" in prompts[0]  # the agreed term was sent with the page
