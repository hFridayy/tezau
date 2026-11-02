"""MCP server: the translator as tools for Claude (Desktop, Claude Code) or any other MCP client.

  "How much would translating /home/me/rules.docx cost?"      -> estimate
  "Translate it into Kazakh"                                  -> translate (runs in the background)
  "How far along is it?"                                      -> job_status
  "Check the translation"                                     -> check
  "заявка should always be өтінім - remember that"            -> term_set
  "Translate this sentence: ..."                              -> translate_text

The same engine, memory and glossary as the `translator` command. Keys, the database and
prices.json live in one folder, TRANSLATOR_HOME (default ~/.kazakh-translator), because an MCP
client starts the server from a folder of its own choosing. Set TRANSLATOR_HOME to your project
folder to share memory and terms with the command line.

Run with:  translator-mcp   (the client starts it; it talks MCP over stdin/stdout)
"""
import functools
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from docx.opc.exceptions import PackageNotFoundError
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import evaluation
from .cli import cost, document_texts, load_prices
from .docx_io import normalize
from .engine import CONTEXT_CHARS, PageTranslator, make_pages, translate_document
from .glossary import Glossary, extract_terms, vote_terms
from .memory import Store
from .providers import KEY_ENV, PROVIDERS, ProviderError, get_provider

SOURCE, TARGET = "Russian", "Kazakh (Cyrillic script)"
DEFAULT_PROVIDER = "gemini"  # fewest problems and best term consistency in our measurements, free tier

server = MCPServer(
    "kazakh-translator",
    instructions=(
        "Translates Russian Word documents (.docx) into Kazakh page by page, keeping the formatting, "
        "with a local translation memory and an agreed glossary. Suggested flow: estimate -> translate -> "
        "poll job_status every 20-30 seconds until it is done -> check. File paths must be full paths. "
        "Human glossary decisions (term_set) always win over the models."),
)


EXPECTED_ERRORS = (ValueError, FileNotFoundError, PackageNotFoundError, ProviderError)


def tool(fn):
    """Register a tool. Expected problems (wrong path, missing key, not a .docx file) reach the
    client with their message; anything else is a bug and shows up only in the server log."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except EXPECTED_ERRORS as e:
            raise ToolError(str(e)) from e
    return server.tool()(wrapper)


# ------------------------------------------------------------------ settings

def home() -> Path:
    path = Path(os.environ.get("TRANSLATOR_HOME", "~/.kazakh-translator")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup() -> None:
    """Load keys from TRANSLATOR_HOME/.env and write reports there."""
    load_dotenv(home() / ".env")
    evaluation.RESULTS_DIR = str(home() / "results")


def db_path() -> str:
    return os.environ.get("TRANSLATOR_DB") or str(home() / "translator.db")


def resolve(path: str, must_exist: bool = True) -> str:
    """Full path from what the user typed. A Windows path (C:\\Users\\...) is converted to WSL's
    /mnt/c/Users/... when the server runs in WSL, since Claude Desktop on Windows sees Windows paths."""
    text = path.strip().strip('"').strip("'")
    drive = re.match(r"^([A-Za-z]):[\\/](.*)$", text)
    if drive and os.name != "nt" and Path("/mnt").is_dir():
        text = f"/mnt/{drive.group(1).lower()}/" + drive.group(2).replace("\\", "/")
    p = Path(text).expanduser()
    if not p.is_absolute():
        raise ValueError(f"Please give the full path, for example /home/you/{text} (got {path!r})")
    if must_exist and not p.exists():
        raise ValueError(f"File not found: {p}")
    return str(p)


def models_from_env() -> dict:
    """Optional TRANSLATOR_MODELS="claude=claude-haiku-5-5,openai=gpt-5-mini" in the .env file."""
    pairs = (item.partition("=") for item in os.environ.get("TRANSLATOR_MODELS", "").split(","))
    return {k.strip(): v.strip() for k, _, v in pairs if k.strip() and v.strip()}


def available_providers() -> list:
    return [name for name in PROVIDERS if os.environ.get(KEY_ENV[name])]


def choose_providers(requested: str = None) -> list:
    """None -> TRANSLATOR_PROVIDERS from .env, else Gemini (or the first provider with a key).
    "all" -> every provider that has a key. "gemini,openai" -> exactly those. "fake" -> free dry run."""
    names = (requested or os.environ.get("TRANSLATOR_PROVIDERS") or "auto").strip().lower()
    have = available_providers()
    if names == "fake":
        from .fake import FakeProvider
        return [FakeProvider()]
    if names == "auto":
        chosen = [DEFAULT_PROVIDER] if DEFAULT_PROVIDER in have else have[:1]
    elif names == "all":
        chosen = have
    else:
        chosen = [n.strip() for n in names.split(",") if n.strip()]
        missing = [n for n in chosen if n not in have]
        if missing:
            raise ValueError(f"No API key for {', '.join(missing)}. Providers with keys: {', '.join(have) or 'none'}")
    if not chosen:
        raise ValueError(f"No API keys found. Add GEMINI_API_KEY (or another key) to {home() / '.env'}")
    models = models_from_env()
    return [get_provider(name, models.get(name)) for name in chosen]


def unfinished(path: str) -> bool:
    """An interrupted run left its progress file next to the output: continuing it is safe."""
    return os.path.exists(path + ".progress.jsonl")


def default_output(input_path: str, dry_run: bool = False) -> str:
    """rusfile.docx -> rusfile_kk.docx, or rusfile_kk_2.docx ... if that exists: never overwrite."""
    p = Path(input_path)
    base = p.stem + ("_dryrun" if dry_run else "_kk")
    candidate, n = p.with_name(base + p.suffix), 1
    while candidate.exists() and not unfinished(str(candidate)):
        n += 1
        candidate = p.with_name(f"{base}_{n}{p.suffix}")
    return str(candidate)


def usage_and_cost(providers: list, pages_by_provider: dict = None) -> dict:
    prices = load_prices(str(home() / "prices.json"))
    report, total = {}, 0.0
    for p in providers:
        if not p.usage.requests:
            continue
        line = {"requests": p.usage.requests, "tokens_in": p.usage.input_tokens,
                "tokens_out": p.usage.output_tokens}
        if pages_by_provider is not None:
            line["pages"] = pages_by_provider.get(p.name, 0)
        if p.model in prices:
            line["usd"] = round(cost(p.usage, prices[p.model]), 4)
            total += line["usd"]
        report[repr(p)] = line
    return {"by_model": report, "total_usd": round(total, 4) if total else None}


# ------------------------------------------------------------------ background jobs

@dataclass
class Job:
    id: str
    input: str
    output: str
    providers: list
    status: str = "starting"     # starting | voting on terms | translating | done | failed
    pages_done: int = 0
    pages_total: int = 0
    started: float = field(default_factory=time.monotonic)
    finished: float = None
    result: dict = None
    error: str = None


JOBS = {}
JOBS_LOCK = threading.Lock()


def run_job(job: Job, vote: bool, notes: str) -> None:
    dry_run = any(p.name == "fake" for p in job.providers)
    store = None if dry_run else Store(db_path())
    try:
        decisions = []
        if store and vote:
            job.status = "voting on terms"
            known = store.terms()
            terms = [t for t in extract_terms(document_texts(job.input))
                     if t not in known]  # new terms only; human decisions are never re-voted
            decisions = vote_terms(terms, job.providers, store=store)
        glossary = Glossary.from_store(store) if store else None
        job.status = "translating"

        def progress(page, provider, done, total):
            job.pages_done, job.pages_total = done, total

        r = translate_document(job.input, job.output, job.providers, store=store, glossary=glossary,
                               notes=notes, on_page_done=progress)
        job.result = {
            "output": r.output, "paragraphs": r.paragraphs, "pages_translated": r.pages,
            "from_memory": r.from_memory, "seconds": round(r.seconds),
            "failed_providers": r.failed,
            "new_terms": {"agreed": sum(d["status"] == "agreed" for d in decisions),
                          "disputed": [d["source"] for d in decisions if d["status"] == "disputed"]},
            **usage_and_cost(job.providers, r.pages_by_provider),
        }
        job.status = "done"
    except Exception as e:  # report every failure to the client instead of losing it in a thread
        job.status, job.error = "failed", f"{type(e).__name__}: {e}"
    finally:
        job.finished = time.monotonic()
        if store:
            store.close()


# ------------------------------------------------------------------ tools

@tool
def estimate(input_path: str) -> dict:
    """Size, cost and time of translating a .docx file, before spending anything.
    Paragraphs already in the translation memory are free and are not counted."""
    path = resolve(input_path)
    texts = list(dict.fromkeys(document_texts(path)))
    cached = Store(db_path()).tm_get_many(texts) if os.path.exists(db_path()) else {}
    todo = [t for t in texts if t not in cached]
    pages = make_pages(todo, 2500)
    chars = sum(len(t) for t in todo)
    tokens_in = chars / 3 + len(pages) * (450 + CONTEXT_CHARS / 3)
    tokens_out = chars / 2.5
    prices = load_prices(str(home() / "prices.json"))
    return {
        "file": path, "unique_paragraphs": len(texts), "already_in_memory": len(cached),
        "pages_to_translate": len(pages), "characters": chars,
        "estimated_tokens": {"in": round(tokens_in), "out": round(tokens_out)},
        "estimated_usd": {m: round((tokens_in * a + tokens_out * b) / 1e6, 3) for m, (a, b) in prices.items()},
        "estimated_minutes_one_provider": round(len(pages) * 2.5 / 60, 1),
        "providers_with_keys": available_providers(),
    }


@tool
def translate(input_path: str, output_path: str = None, providers: str = None,
              vote_terms_first: bool = False, notes: str = "", overwrite: bool = False) -> dict:
    """Start translating a Russian .docx file into Kazakh in the background; returns a job id at once.
    Then call job_status(job_id) every 20-30 seconds until status is "done".

    output_path: default is the input name + "_kk.docx" in the same folder (_kk_2.docx ... if that
      exists; _dryrun.docx for the fake provider). Existing files are never replaced unless overwrite=true.
    providers: omit for the default (Gemini); "all" for every provider with a key (parallel, with
      failover); or a list like "gemini,openai"; "fake" for a free dry run.
    vote_terms_first: let the models vote on key terms before translating (recommended for a new topic).
    notes: extra instructions for the models, e.g. "medical simulation centre documentation".
    Unchanged paragraphs come from the translation memory for free; an interrupted run continues
    where it stopped when started again with the same files."""
    source = resolve(input_path)
    chosen = choose_providers(providers)
    dry_run = any(p.name == "fake" for p in chosen)
    output = resolve(output_path, must_exist=False) if output_path else default_output(source, dry_run)
    if os.path.exists(output) and not overwrite and not unfinished(output):
        raise ValueError(f"{output} already exists. Give another output_path, or overwrite=true to replace it")
    with JOBS_LOCK:
        for other in JOBS.values():
            if other.output == output and other.status not in ("done", "failed"):
                raise ValueError(f"Job {other.id} is already writing {output}")
        job = Job(uuid.uuid4().hex[:8], source, output, chosen)
        JOBS[job.id] = job
    threading.Thread(target=run_job, args=(job, vote_terms_first, notes), daemon=True).start()
    return {"job_id": job.id, "output": output, "providers": [repr(p) for p in job.providers],
            "next": "call job_status with this job_id in 20-30 seconds"}


@tool
def job_status(job_id: str) -> dict:
    """Progress of a translation job; when done, the result with pages, time and cost."""
    job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"Unknown job {job_id!r}. Jobs in this session: {', '.join(JOBS) or 'none'}")
    status = {"job_id": job.id, "status": job.status, "output": job.output,
              "pages_done": job.pages_done, "pages_total": job.pages_total,
              "seconds": round((job.finished or time.monotonic()) - job.started)}  # stops when done
    if job.result:
        status["result"] = job.result
    if job.error:
        status["error"] = job.error
        status["hint"] = "Finished pages are saved; call translate again with the same files to continue."
    return status


@tool
def check(source_path: str, translation_path: str, judge: str = None, sample: int = 30) -> str:
    """Quality report: automatic checks on every paragraph (untranslated, half-translated, glued JSON,
    mixed alphabets, changed numbers ...) and which model wrote the problems.
    judge: optional provider name (e.g. "claude") for an AI judge with MQM error scores on `sample`
    random paragraphs - use a different model than the translator. The full report is saved as a file."""
    src, tgt = resolve(source_path), resolve(translation_path)
    judge_provider = choose_providers(judge)[0] if judge else None
    store = Store(db_path()) if os.path.exists(db_path()) else None
    return evaluation.run_check(src, tgt, judge_provider, sample, store=store)


@tool
def translate_text(text: str, providers: str = None) -> dict:
    """Translate a short Russian text (a sentence or a few paragraphs, one per line) into Kazakh,
    using the translation memory and the agreed glossary. Good for a quick question or one paragraph."""
    lines = [normalize(line) for line in text.splitlines() if normalize(line)]
    if not lines:
        raise ValueError("No text to translate")
    if sum(map(len, lines)) > 10000:
        raise ValueError("Text is long - save it as a .docx file and use the translate tool")
    provider_list = choose_providers(providers)
    store = None if provider_list[0].name == "fake" else Store(db_path())
    cached = store.tm_get_many(lines) if store else {}
    todo = [s for s in lines if s not in cached]
    translated = dict(cached)
    if todo:
        glossary = Glossary.from_store(store) if store else None
        engine = PageTranslator(provider_list[0], SOURCE, TARGET, glossary=glossary)
        translated.update(zip(todo, engine.translate(todo)))
        if store:
            store.tm_put_many([(s, translated[s], provider_list[0].name) for s in todo])
    return {"translation": "\n".join(translated[s] for s in lines), "from_memory": len(cached),
            "model": repr(provider_list[0]) if todo else "translation memory"}


@tool
def terms_list(status: str = None) -> list:
    """The agreed glossary: Russian term -> Kazakh term, with each model's vote.
    status: optional filter - agreed, disputed (the models disagreed: a human should decide), single, human."""
    if not os.path.exists(db_path()):
        return []
    return [r for r in Store(db_path()).term_rows() if not status or r["status"] == status]


@tool
def term_set(source: str, target: str) -> dict:
    """Save a human glossary decision, e.g. source="заявки", target="өтінімдер".
    Models never change it, and every later translation uses it. Paragraphs already translated
    keep their old wording until they are translated again."""
    Store(db_path()).term_put(source.strip(), target.strip(), status="human")
    return {"saved": f"{source.strip()} -> {target.strip()}", "status": "human"}


@server.resource("glossary://terms", mime_type="text/plain")
def glossary_resource() -> str:
    """The glossary as plain text, one 'Russian -> Kazakh [status]' per line."""
    return "\n".join(f"{r['source']} -> {r['target']} [{r['status']}]" for r in terms_list()) or "(no terms yet)"


def main() -> None:
    setup()
    server.run()  # stdio: the client starts this process and talks to it over stdin/stdout


if __name__ == "__main__":
    main()
