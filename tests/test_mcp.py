"""The MCP server, tested through a real MCP client connected in-process (no subprocess, no keys)."""
import asyncio
import json
import time

import pytest

pytest.importorskip("mcp")
from mcp import Client  # noqa: E402

from translator import mcp_server as ms  # noqa: E402
from translator.docx_io import iter_paragraphs, open_document  # noqa: E402


@pytest.fixture(autouse=True)
def translator_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSLATOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TRANSLATOR_PROVIDERS", raising=False)
    ms.setup()
    ms.JOBS.clear()


def call(tool, **arguments):
    """Call a tool the way Claude does and return its parsed result."""
    async def go():
        async with Client(ms.server) as client:
            return await client.call_tool(tool, arguments)
    result = asyncio.run(go())
    text = result.content[0].text if result.content else ""
    if result.is_error:
        raise RuntimeError(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def test_tools_are_listed_with_descriptions():
    async def go():
        async with Client(ms.server) as client:
            return await client.list_tools()
    tools = {t.name: t for t in asyncio.run(go()).tools}
    assert {"estimate", "translate", "job_status", "check", "translate_text", "terms_list", "term_set"} <= set(tools)
    assert all(t.description for t in tools.values())


def test_translate_job_end_to_end(make_docx, tmp_path):
    src = make_docx(["Договор поставки товара.", "Пункт второй договора."])
    started = call("translate", input_path=src, providers="fake")
    assert started["output"].endswith("in_dryrun.docx")
    for _ in range(100):
        status = call("job_status", job_id=started["job_id"])
        if status["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert status["status"] == "done", status
    assert status["result"]["paragraphs"] == 2
    time.sleep(1.1)
    assert call("job_status", job_id=started["job_id"])["seconds"] == status["seconds"]  # clock stopped
    assert [p.text for p in iter_paragraphs(open_document(started["output"]))][0] == "[қаз] Договор поставки товара."

    report = call("check", source_path=src, translation_path=started["output"])
    assert "100.0%" in report


def test_never_overwrites_without_asking(make_docx, tmp_path):
    src = make_docx(["Договор поставки товара."])
    (tmp_path / "in_kk.docx").write_text("a real translation")
    assert ms.default_output(src).endswith("in_kk_2.docx")
    assert ms.default_output(src, dry_run=True).endswith("in_dryrun.docx")
    with pytest.raises(RuntimeError, match="already exists"):
        call("translate", input_path=src, output_path=str(tmp_path / "in_kk.docx"), providers="fake")
    (tmp_path / "in_kk.docx.progress.jsonl").write_text("{}")  # an interrupted run may continue
    assert call("translate", input_path=src, output_path=str(tmp_path / "in_kk.docx"), providers="fake")["job_id"]


def test_estimate_and_errors_are_clear(make_docx):
    est = call("estimate", input_path=make_docx(["Договор поставки товара."]))
    assert est["unique_paragraphs"] == 1 and est["pages_to_translate"] == 1
    with pytest.raises(RuntimeError, match="full path"):
        call("estimate", input_path="rusfile.docx")
    with pytest.raises(RuntimeError, match="not found"):
        call("estimate", input_path="/nowhere/rusfile.docx")
    with pytest.raises(RuntimeError, match="Unknown job"):
        call("job_status", job_id="nope")


def test_terms_and_resource():
    assert call("term_set", source="заявки", target="өтінімдер")["status"] == "human"
    terms = call("terms_list")
    terms = terms if isinstance(terms, list) else [terms]
    assert terms[0]["target"] == "өтінімдер"

    async def go():
        async with Client(ms.server) as client:
            return await client.read_resource("glossary://terms")
    assert "заявки -> өтінімдер [human]" in asyncio.run(go()).contents[0].text


def test_translate_text_dry_run():
    result = call("translate_text", text="Договор поставки.\nВторой пункт.", providers="fake")
    assert result["translation"] == "[қаз] Договор поставки.\n[қаз] Второй пункт."


def test_provider_choice(monkeypatch):
    # stand-in providers: the AI libraries are not installed in CI
    monkeypatch.setattr(ms, "get_provider", lambda name, model=None: type("P", (), {"name": name})())
    with pytest.raises(ValueError, match="No API keys"):
        ms.choose_providers()
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    assert [p.name for p in ms.choose_providers()] == ["openai"]       # no Gemini key: first available
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    assert [p.name for p in ms.choose_providers()] == ["gemini"]       # the default
    assert [p.name for p in ms.choose_providers("all")] == ["gemini", "openai"]
    with pytest.raises(ValueError, match="No API key for claude"):
        ms.choose_providers("claude")


def test_windows_paths_become_wsl_paths(monkeypatch):
    monkeypatch.setattr(ms.os, "name", "posix")
    monkeypatch.setattr(ms.Path, "is_dir", lambda self: str(self) == "/mnt" or False)
    assert ms.resolve(r"C:\Users\Me\Docs\rusfile.docx", must_exist=False) == "/mnt/c/Users/Me/Docs/rusfile.docx"
