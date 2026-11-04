"""Answer validation. Each case is modelled on a real problem (or a real false alarm) found while
translating a 75-page document; the sentences are invented, the patterns are the real ones.

The rules were tuned five times (updates 26c-26g); these tests make sure a fix for one
case never silently breaks another.
"""
import pytest

from tezau.engine import detect_problems, is_bad, looks_untranslated, repair

# (Russian source, model output, expected problems)
REAL_CASES = [
    # --- real problems: the model must translate these again ---------------------------------
    pytest.param(
        "Данные о бронировании переговорной комнаты в календаре должны отображаться в разделе "
        "«Отчеты» для анализа загрузки.",
        "Данные о бронировании переговорной бөлме календарь ішінде жүктемені талдау үшін «Есептер» "
        "бөлімінде көрсетілуі тиіс.",
        ["half_translated"], id="half Russian, broken up by 'о' (the case the AI judge found)"),
    pytest.param(
        "После обновления любого правила система должна пересчитать график и уведомить сотрудников.",
        "После обновления любого правила система должна пересчитать график и уведомить сотрудников.",
        ["untranslated"], id="left completely in Russian"),
    pytest.param(
        "Настройка уведомлений для сотрудников отдела",
        'Настройка уведомлений для сотрудников отдела": "Бөлім қызметкерлері үшін хабарламаларды баптау',
        ["json_leak", "half_translated"], id="source and translation glued together"),
    pytest.param(
        "Сотрудник не должен иметь доступ к архиву.",
        "Қызметкердің мұрағатқа кіругe рұқсаты болмауы тиіс.",
        ["mixed_script"], id="Latin e inside a Kazakh word"),
    pytest.param("● склада;", "● склада;", ["unchanged"], id="short line left as in Russian"),
    pytest.param("● доступных ресурсов", "● доступных ресурстар", ["half_translated"],
                 id="short line, Russian ending"),

    # --- correct translations: must NOT be flagged (false alarms we fixed) -------------------
    pytest.param("● принтер/сканер/плоттер — если используется.", "● принтер/сканер/плоттер — егер қолданылса.",
                 [], id="shared loanwords"),
    pytest.param("При этом кабинет и принтер/сканер являются необязательными параметрами.",
                 "Бұл ретте кабинет пен принтер/сканер міндетті емес параметрлер болып табылады.",
                 [], id="loanwords in a long sentence"),
    pytest.param("● «Логотип» — загрузка изображения в форматах PNG, SVG;",
                 "● «Логотип» — PNG, SVG форматтарында сурет жүктеу;", [], id="Latin file formats"),
    pytest.param("● Оборудование (ноутбук, проектор, маршрутизатор, монитор, принтер, сканер).",
                 "● Жабдықтар (ноутбук, проектор, маршрутизатор, монитор, принтер, сканер).",
                 [], id="device names"),
    pytest.param("REQ-12. API для внешних систем должен иметь документацию, авторизацию, ограничения "
                 "доступа, версии и понятные коды ошибок.",
                 "REQ-12. Сыртқы жүйелерге арналған API құжаттама, авторизация, қолжетімділік шектеулері, "
                 "нұсқалар және түсінікті қате кодтары болуы тиіс.", [], id="API and codes"),
    pytest.param("Данные о бронировании переговорной комнаты в календаре должны отображаться в разделе "
                 "«Отчеты» для анализа загрузки.",
                 "Күнтізбедегі келіссөз бөлмесін брондау туралы деректер жүктемені талдау үшін «Есептер» "
                 "бөлімінде көрсетілуі тиіс.", [], id="the half-Russian case, corrected"),
    pytest.param("[тест]", "[қаз] [тест]", [], id="dry-run marker is never flagged"),
]


@pytest.mark.parametrize("source, translation, expected", REAL_CASES)
def test_real_cases(source, translation, expected):
    assert sorted(detect_problems(source, translation)) == sorted(expected)


@pytest.mark.parametrize("word", ["Студент", "Инженер", "Функция", "● курс;", "6. Кабинет", "4.2 Мониторинг"])
def test_loanwords_are_notes_not_errors(word):
    """Same as in Russian, but correct Kazakh: noted for review, trusted in memory."""
    assert detect_problems(word, word) == ["unchanged"]
    assert not is_bad(word, word)


def test_real_problems_are_bad():
    assert is_bad("● доступных ресурсов", "● доступных ресурстар")


def test_looks_untranslated():
    assert looks_untranslated("Договор поставки товара", "Договор поставки товара")
    assert not looks_untranslated("Договор поставки товара", "Тауар жеткізу шарты")
    assert not looks_untranslated("Модуль", "Модуль")  # one word: may be a correct loanword


def test_repair_glued_json_and_lookalikes():
    source = "Настройка параметров доступа"
    assert repair(source, 'Настройка параметров доступа": "Қолжетімділік параметрлерін баптау') \
        == "Қолжетімділік параметрлерін баптау"
    assert repair("Правила", "Ережe") == "Ереже"  # Latin e -> Cyrillic е
    assert repair("Формат", "PNG форматы") == "PNG форматы"  # real Latin words stay
