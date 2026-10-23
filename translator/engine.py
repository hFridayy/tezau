"""The translation engine: document -> pages -> AI -> checked translations -> document.

Pipeline (a map-reduce / fan-out, fan-in pattern):
1. read   - collect the paragraphs that need translating, without duplicates
2. split  - group them into "pages" of about page_chars characters (chunking)
3. map    - one or several providers take small blocks of pages from a shared queue;
            if a provider fails, the others take over its pages (failover)
4. save   - every finished page is appended to a progress file (checkpointing)
5. reduce - put the translations back into the original document and save it
"""
import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .docx_io import iter_paragraphs, needs_translation, normalize, open_document, set_paragraph_text
from .providers import Provider, ProviderError

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a professional translator from {source} to {target}.
You receive one page of a document as a JSON object: keys are paragraph numbers, values are paragraphs.
Return ONLY a JSON object with exactly the same keys, each value being the translation of that paragraph.
Rules:
- Translate every paragraph completely and accurately; do not summarize, merge, split or skip paragraphs.
- Keep numbers, dates, codes, URLs, e-mail addresses and list numbering unchanged.
- Use natural, correct language and consistent terminology across the whole document.
- No comments, no explanations, no markdown - only the JSON object."""

CONTEXT_CHARS = 800  # how much of the previous page to show the model

# Letters that exist in Kazakh but not in Russian. A Kazakh sentence of 8+ words almost
# always contains one of them; a Russian sentence never does.
KAZAKH_ONLY = set("әғқңөұүһіӘҒҚҢӨҰҮҺІ")


def looks_untranslated(source: str, translation: str) -> bool:
    """True if the model most likely returned the text still in Russian."""
    src, out = normalize(source), normalize(translation)
    if out == src and len(src.split()) >= 3:
        return True
    return len(out.split()) >= 8 and not KAZAKH_ONLY.intersection(out)


# ---------------------------------------------------------------- chunking

@dataclass
class Piece:
    paragraph: str  # the full original paragraph
    text: str       # the part of it sent to the model (the whole paragraph unless it is very long)


def split_long(text: str, max_chars: int) -> list:
    """Split one very long paragraph at sentence ends (or spaces) into parts of <= max_chars."""
    parts, current = [], ""
    for sentence in re.split(r"(?<=[.!?…;])\s+", text):
        while len(sentence) > max_chars:  # a single sentence longer than a page
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            if current:
                parts.append(current)
                current = ""
            parts.append(sentence[:cut])
            sentence = sentence[cut:].lstrip()
        if current and len(current) + 1 + len(sentence) > max_chars:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current:
        parts.append(current)
    return parts


def make_pages(paragraphs: list, page_chars: int) -> list:
    """Group paragraphs into pages of about page_chars characters. O(n)."""
    pages, page, size = [], [], 0
    for paragraph in paragraphs:
        texts = split_long(paragraph, page_chars) if len(paragraph) > page_chars else [paragraph]
        for text in texts:
            if page and size + len(text) > page_chars:
                pages.append(page)
                page, size = [], 0
            page.append(Piece(paragraph, text))
            size += len(text)
    if page:
        pages.append(page)
    return pages


# ---------------------------------------------------------------- talking to the model

def parse_json_object(text: str) -> dict:
    """Models sometimes wrap JSON in ```json fences or add a sentence - take the {...} part."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in the answer")
    data, _ = json.JSONDecoder().raw_decode(text[start:])  # the first complete object; ignore extra text
    if not isinstance(data, dict):
        raise ValueError("the answer is not a JSON object")
    return data


class PageTranslator:
    """Translates pages one after another, remembering the end of the previous page."""

    def __init__(self, provider: Provider, source: str, target: str, notes: str = "", glossary=None):
        self.provider = provider
        self.glossary = glossary
        self.source, self.target = source, target
        self.system = SYSTEM_PROMPT.format(source=source, target=target)
        if notes:
            self.system += f"\nAdditional instructions: {notes}"
        self.context = ""

    def translate(self, texts: list, _depth: int = 0) -> list:
        """Translate a list of paragraphs; always returns a list of the same length."""
        payload = {str(i + 1): t for i, t in enumerate(texts)}
        user = ""
        if self.context:
            user += ("For consistency, this is how the previous page ended (already translated, "
                     f"do NOT translate it again):\n{self.context}\n\n")
        terms = self.glossary.for_text(" ".join(texts)) if self.glossary else []
        if terms:  # only the agreed terms that appear on this page
            user += ("Use these agreed term translations, inflected as the grammar requires:\n"
                     + "\n".join(f"{s} -> {t}" for s, t in terms) + "\n\n")
        user += "Translate this page:\n" + json.dumps(payload, ensure_ascii=False, indent=0)

        for attempt in range(2):
            try:
                data = parse_json_object(self.provider.complete(self.system, user))
                result = [str(data[key]).strip() for key in payload]
                if all(result):
                    bad = [i for i, (s, t) in enumerate(zip(texts, result)) if looks_untranslated(s, t)]
                    if bad and _depth < 2:  # translate only the untranslated paragraphs again
                        log.warning("%d of %d paragraphs came back untranslated, translating them again",
                                    len(bad), len(texts))
                        for i, fixed in zip(bad, self.translate([texts[i] for i in bad], _depth + 1)):
                            result[i] = fixed
                    elif bad:
                        log.warning("%d paragraphs still look untranslated after retries", len(bad))
                    self.context = "\n".join(result)[-CONTEXT_CHARS:]
                    return result
                problem = "an empty translation"
            except (ValueError, KeyError) as e:  # ValueError includes json.JSONDecodeError
                problem = f"{type(e).__name__}: {e}"
            log.warning("answer for %d paragraphs had %s, retrying", len(texts), problem)

        if len(texts) == 1:  # last resort for one paragraph: plain text, no JSON
            answer = self.provider.complete(
                f"Translate the user's text from {self.source} to {self.target}. "
                "Reply with the translation only.", texts[0], json_mode=False).strip()
            return [answer or texts[0]]
        half = len(texts) // 2  # smaller requests fail less often
        return self.translate(texts[:half], _depth) + self.translate(texts[half:], _depth)


# ---------------------------------------------------------------- checkpointing

class Checkpoint:
    """Progress file in JSON Lines format: a header line, then one line per finished page.

    Appending one line per page is O(1) per save (rewriting the whole file each time
    would be O(pages) per save, O(pages^2) in total). If the run crashes in the middle
    of writing, only the last, broken line is lost.
    """

    def __init__(self, path: str, fingerprint: str):
        self.path = path
        self.done = {}
        self.done_by = {}  # page -> provider name
        self._lock = threading.Lock()
        if os.path.exists(path):
            self._load(fingerprint)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"fingerprint": fingerprint}) + "\n")

    def _load(self, fingerprint: str) -> None:
        records = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # half-written line from a crash
        if records and records[0].get("fingerprint") == fingerprint:
            for record in records[1:]:
                self.done[record["page"]] = record["translations"]
                self.done_by[record["page"]] = record.get("provider", "")
            log.info("resuming: %d pages already translated", len(self.done))
        else:
            log.info("progress file belongs to another document or settings - starting over")
            os.remove(self.path)

    def save(self, page: int, translations: list, provider: str = "") -> None:
        with self._lock:
            self.done[page] = translations
            self.done_by[page] = provider
            record = {"page": page, "provider": provider, "translations": translations}
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def remove(self) -> None:
        if os.path.exists(self.path):
            os.remove(self.path)


def fingerprint(pages: list, source: str, target: str) -> str:
    """Identifies the job, so a progress file is only reused for the same document and settings."""
    data = json.dumps([source, target, [[p.text for p in page] for page in pages]], ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- the whole pipeline

@dataclass
class Result:
    output: str
    paragraphs: int
    pages: int
    seconds: float
    usage: dict            # provider name -> Usage
    pages_by_provider: dict  # provider name -> number of pages it translated in this run
    failed: dict           # provider name -> error message, for providers that dropped out
    from_memory: int = 0   # paragraphs reused from the translation memory (no API cost)


def make_blocks(page_numbers: list, block_pages: int) -> list:
    """Cut the page numbers into small continuous blocks: [1..5], [6..10], ...
    A block is the unit of work a provider takes from the queue."""
    return [page_numbers[i:i + block_pages] for i in range(0, len(page_numbers), block_pages)]


def translate_document(input_path: str, output_path: str, providers, *,
                       workers: int = 1, block_pages: int = 5, page_chars: int = 2500,
                       notes: str = "", source: str = "Russian", target: str = "Kazakh (Cyrillic script)",
                       store=None, glossary=None, max_pages: int = None, on_page_done=None) -> Result:
    """Translate a .docx with one or several providers sharing the work.

    The pages are cut into blocks of `block_pages` and put in a queue. Every provider runs
    `workers` threads; each thread takes the next block, translates it, takes another.
    Fast providers end up doing more blocks (dynamic load balancing). If a provider fails
    for good (bad key, no credit, quota used up), its unfinished pages go back to the queue
    and the remaining providers finish them (failover).

    max_pages: translate only the first N pages (for cheap tests); the rest stays untranslated.
    store: a memory.Store - paragraphs it already knows are reused for free, new ones are added.
    glossary: a glossary.Glossary - every page gets the agreed terms that appear on it.
    on_page_done(page_number, provider_name, pages_done, pages_total) is called after each page.
    """
    if isinstance(providers, Provider):
        providers = [providers]
    started = time.monotonic()

    # 1. read
    doc = open_document(input_path)
    paragraphs = [p for p in iter_paragraphs(doc) if needs_translation(p.text)]
    unique = list(dict.fromkeys(normalize(p.text) for p in paragraphs))  # O(n), keeps order

    # 2. reuse the translation memory, split the rest into pages
    cached = store.tm_get_many(unique) if store else {}
    cached = {s: t for s, t in cached.items() if not looks_untranslated(s, t)}  # don't reuse bad answers
    todo = [text for text in unique if text not in cached]
    pages = make_pages(todo, page_chars)
    if max_pages:
        pages = pages[:max_pages]
    checkpoint = Checkpoint(output_path + ".progress.jsonl", fingerprint(pages, source, target))
    log.info("%d paragraphs (%d unique, %d from memory) -> %d pages; providers: %s, %d worker(s) each",
             len(paragraphs), len(unique), len(cached), len(pages),
             ", ".join(map(repr, providers)), workers)

    # 3 + 4. map: workers take blocks from a shared queue, saving each page as it finishes
    stop = threading.Event()
    lock = threading.Lock()
    failed = {}                      # provider name -> error message
    pages_by = {p.name: 0 for p in providers}

    def translate_block(provider, block):
        translator = PageTranslator(provider, source, target, notes, glossary)
        previous = checkpoint.done.get(block[0] - 1)
        if previous:  # the page before this block is done: use it as context
            translator.context = "\n".join(previous)[-CONTEXT_CHARS:]
        for i in block:
            if stop.is_set():
                return
            if i in checkpoint.done:
                translator.context = "\n".join(checkpoint.done[i])[-CONTEXT_CHARS:]
                continue
            checkpoint.save(i, translator.translate([piece.text for piece in pages[i - 1]]), provider.name)
            with lock:
                pages_by[provider.name] += 1
            if on_page_done:
                on_page_done(i, provider.name, len(checkpoint.done), len(pages))

    def worker(provider, work):
        while not stop.is_set():
            with lock:
                if provider.name in failed:
                    return
            try:
                block = work.get_nowait()
            except queue.Empty:
                return
            try:
                translate_block(provider, block)
            except ProviderError as e:  # includes AuthError: this provider is out
                with lock:
                    if provider.name not in failed:
                        failed[provider.name] = str(e)
                        log.warning("%r dropped out: %s - other providers take over its pages", provider, e)
                return

    # Rounds: a block returned by a failing provider is picked up in the next round.
    while True:
        missing = [i for i in range(1, len(pages) + 1) if i not in checkpoint.done]
        healthy = [p for p in providers if p.name not in failed]
        if not missing or not healthy:
            break
        work = queue.Queue()
        for block in make_blocks(missing, block_pages):
            work.put(block)
        threads = [(p, n) for p in healthy for n in range(workers)]
        with ThreadPoolExecutor(max_workers=len(threads)) as pool:
            futures = [pool.submit(worker, p, work) for p, _ in threads]
            try:
                for future in futures:
                    future.result()  # re-raises unexpected errors (bugs), not provider errors
            except BaseException:    # a bug or Ctrl+C: let the others finish their current page
                stop.set()
                log.warning("stopping - finished pages are saved; run again to continue")
                raise

    missing = [i for i in range(1, len(pages) + 1) if i not in checkpoint.done]
    if missing:
        reasons = "; ".join(f"{name}: {msg}" for name, msg in failed.items())
        raise ProviderError(f"all providers failed with {len(missing)} of {len(pages)} pages left "
                            f"(finished pages are saved, run again to continue). {reasons}")

    # 5. reduce: put translations back in document order
    translated, made_by = {}, {}
    for i, page in enumerate(pages, 1):
        result = checkpoint.done[i]
        if len(result) != len(page):
            raise RuntimeError(f"progress file is damaged at page {i}; delete {checkpoint.path}")
        for piece, text in zip(page, result):
            translated[piece.paragraph] = f"{translated[piece.paragraph]} {text}" \
                if piece.paragraph in translated else text
            made_by[piece.paragraph] = checkpoint.done_by.get(i, "")
    if store:  # remember the new translations for next time
        store.tm_put_many([(s, t, made_by[s]) for s, t in translated.items()])
    translated.update(cached)
    for p in paragraphs:
        key = normalize(p.text)
        if key in translated:  # with max_pages, later paragraphs stay as they are
            set_paragraph_text(p, translated[key])
    doc.save(output_path)
    checkpoint.remove()
    return Result(output_path, len(paragraphs), len(pages), time.monotonic() - started,
                  {repr(p): p.usage for p in providers}, pages_by, failed, len(cached))
