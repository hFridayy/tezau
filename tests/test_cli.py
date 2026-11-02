"""The `translator` command, run the way a user runs it (with the fake provider)."""
import json

from translator.cli import main
from translator.docx_io import iter_paragraphs, open_document


def test_dry_run_translate_and_check(make_docx, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # reports are written to ./results
    src = make_docx(["Договор поставки товара.", "Пункт второй договора поставки."])
    out = str(tmp_path / "out.docx")

    assert main(["translate", src, out, "--providers", "fake"]) == 0
    assert [p.text for p in iter_paragraphs(open_document(out))][0] == "[қаз] Договор поставки товара."
    assert not (tmp_path / "translator.db").exists()  # dry runs never touch the memory

    assert main(["eval", "check", src, out]) == 0
    report = json.loads(next((tmp_path / "results").glob("check_*.json")).read_text(encoding="utf-8"))
    assert report["checked"] == 2 and report["pass_rate"] == 100.0


def test_terms_set_and_list(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["terms", "set", "заявки", "өтінімдер"]) == 0
    assert main(["terms", "list"]) == 0
    assert "өтінімдер" in capsys.readouterr().out
