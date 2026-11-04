"""Shared test helpers: fake AI providers and small .docx files. No API keys, no network, no cost."""
import json
import threading

import pytest
from docx import Document

import tezau.providers as providers_module
from tezau.providers import Provider


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Retries in tests should not sleep."""
    monkeypatch.setattr(providers_module, "backoff_seconds", lambda *a, **k: 0)


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    """Tests never use real keys, even if a .env file is present on this machine."""
    for key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)


class ScriptedProvider(Provider):
    """A fake model. By default it answers every page with '[қаз] <text>'.

    translate: function(text) -> translation, to script special answers
    fail_after: raise an error after this many successful page requests
    error: the exception to raise ('API key not valid' = auth, 'credit balance' = fatal ...)
    """

    def __init__(self, name="fake-a", translate=None, fail_after=None, error=None, terms=None):
        self.name = name
        self.script = translate or (lambda text: f"[қаз] {text}")
        self.fail_after = fail_after
        self.error = error or PermissionError("API key not valid (test)")
        self.term_answers = terms or {}
        self.pages_seen = []
        self.lock = threading.Lock()
        super().__init__(model="test", api_key="test")

    def _call(self, system, user, json_mode):
        if system.startswith("You are a terminologist"):
            answer = {t: self.term_answers.get(t, f"[қаз] {t}") for t in json.loads(user)}
            return json.dumps(answer, ensure_ascii=False), 5, 5
        with self.lock:
            if self.fail_after is not None and len(self.pages_seen) >= self.fail_after:
                raise self.error
        if "Translate this page:\n" in user:
            page = json.loads(user.split("Translate this page:\n", 1)[1])
            with self.lock:
                self.pages_seen.append(page)
            answer = {k: self.script(v) for k, v in page.items()}
            return json.dumps(answer, ensure_ascii=False), 10, 10
        return self.script(user), 5, 5  # plain-text fallback for one paragraph


@pytest.fixture
def make_provider():
    return ScriptedProvider


@pytest.fixture
def make_docx(tmp_path):
    """make_docx(["paragraph", ...], table=[["cell", ...]], header="...") -> path"""
    def build(paragraphs, name="in.docx", table=None, header=None, bold_first_word=False):
        doc = Document()
        for text in paragraphs:
            p = doc.add_paragraph()
            if bold_first_word and " " in text:
                first, rest = text.split(" ", 1)
                p.add_run(first + " ").bold = True
                p.add_run(rest)
            else:
                p.add_run(text)
        if table:
            t = doc.add_table(rows=len(table), cols=len(table[0]))
            for r, row in enumerate(table):
                for c, text in enumerate(row):
                    t.cell(r, c).text = text
        if header:
            doc.sections[0].header.is_linked_to_previous = False
            doc.sections[0].header.paragraphs[0].text = header
        path = tmp_path / name
        doc.save(path)
        return str(path)
    return build
