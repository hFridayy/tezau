"""Offline self-check: which updates are applied, and does the pipeline work? No API calls, no cost."""
import json, os, sys, tempfile, threading

sys.path.insert(0, os.getcwd())  # import the translator package from the folder you run this in

ok_all = True


def check(name, ok, hint=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok or not hint else f"  ->  {hint}"))


print("1. Files and updates")
files = ["__init__", "docx_io", "providers", "engine", "memory", "glossary"]
for f in files:
    check(f"translator/{f}.py exists", os.path.exists(f"translator/{f}.py"), "create it (see the step where it was added)")
src = {f: open(f"translator/{f}.py", encoding="utf-8").read() for f in files if os.path.exists(f"translator/{f}.py")}
check("step 2.4b: work queue + failover", "def make_blocks" in src.get("engine", ""), "run update_step24b.py")
check("step 2.4b: fatal errors stop at once", '"fatal"' in src.get("providers", ""), "run update_step24b.py")
check("step 2.4c: memory + glossary in engine", "store=None, glossary=None" in src.get("engine", ""), "run update_step24c.py")
check("step 2.4d: reasoning tokens", "reasoning_tokens" in src.get("providers", ""), "run update_step24d.py")
try:
    import translator.docx_io, translator.providers, translator.engine, translator.memory, translator.glossary
    check("all modules import", True)
except Exception as e:
    check("all modules import", False, f"{type(e).__name__}: {e}")
    sys.exit("\nFix the import error above first.")

print("2. Offline pipeline test (fake AI)")
from docx import Document
import translator.providers as pr
from translator.engine import translate_document
from translator.memory import Store
from translator.glossary import extract_terms, vote_terms, Glossary

pr.backoff_seconds = lambda *a, **k: 0


def fake(name_, broken=False):
    class Fake(pr.Provider):
        name = name_

        def _setup(self):
            self.lock = threading.Lock()

        def _call(self, system, user, json_mode):
            if broken:
                raise PermissionError("API key not valid (fake)")
            if system.startswith("You are a terminologist"):
                return json.dumps({t: "тауар" if "товар" in t else f"KK({t})" for t in json.loads(user)},
                                  ensure_ascii=False), 5, 5
            payload = json.loads(user.split("Translate this page:\n", 1)[1])
            return json.dumps({k: "[қаз] " + v for k, v in payload.items()}, ensure_ascii=False), 10, 8
    return Fake(api_key="fake")


tmp = tempfile.mkdtemp()
doc = Document()
for i in range(120):
    doc.add_paragraph(f"Пункт {i}. Поставщик передает товар покупателю по договору поставки.")
doc.add_paragraph("2026")
doc.save(f"{tmp}/in.docx")
source = [p.text for p in Document(f"{tmp}/in.docx").paragraphs]

store = Store(f"{tmp}/test.db")
terms = extract_terms(source, max_terms=10)
check("terms extracted", len(terms) > 0, str(terms))
decisions = vote_terms(terms, [fake("gemini"), fake("openai")], store=store)
check("models voted on terms", any(d["status"] == "agreed" for d in decisions))
providers = [fake("gemini", broken=True), fake("claude"), fake("openai")]
r = translate_document(f"{tmp}/in.docx", f"{tmp}/out.docx", providers, store=store,
                       glossary=Glossary.from_store(store), block_pages=1)
out = [p.text for p in Document(f"{tmp}/out.docx").paragraphs]
check("every paragraph translated, in the right order",
      all(o == "[қаз] " + s for s, o in zip(source, out) if any(c.isalpha() for c in s)))
check("numbers-only paragraph left alone", out[-1] == "2026")
check("broken provider dropped out, others took over", "gemini" in r.failed and r.pages_by_provider["gemini"] == 0)
r2 = translate_document(f"{tmp}/in.docx", f"{tmp}/out2.docx", [fake("claude")], store=store)
check("second run served from memory (0 pages, no cost)", r2.pages == 0 and r2.from_memory > 0)

print("\nALL CHECKS PASSED - ready for a real test" if ok_all else "\nSome checks FAILED - see the hints above")
