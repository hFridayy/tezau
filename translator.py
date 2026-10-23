"""
Translate a .docx file from Russian to Kazakh.

Usage:
    python translate_docx.py input.docx output.docx            # Google Translate (online, best quality)
    python translate_docx.py input.docx output.docx --offline  # NLLB model (local, no internet)

Install:
    pip install python-docx deep-translator                     # for the online mode
    pip install python-docx transformers torch sentencepiece    # for --offline
"""
import json
import os
import re
import sys
import time

from docx import Document
from docx.oxml.ns import qn

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


# ---------- splitting long text into translatable chunks ----------

def split_chunks(text, max_len):
    sentences = re.split(r"(?<=[.!?…;])\s+", text)
    chunks, cur = [], ""
    for s in sentences:
        if cur and len(cur) + 1 + len(s) > max_len:
            chunks.append(cur)
            cur = ""
        cur = f"{cur} {s}" if cur else s
        while len(cur) > max_len:  # a single very long sentence
            cut = cur.rfind(" ", 0, max_len)
            if cut <= 0:
                cut = max_len
            chunks.append(cur[:cut])
            cur = cur[cut:].lstrip()
    if cur:
        chunks.append(cur)
    return chunks


# ---------- translation backends ----------

class GoogleBackend:
    # Cyrillic text grows ~6x when URL-encoded, so keep requests well under Google's URL limit
    max_len = 1200

    def __init__(self):
        from deep_translator import GoogleTranslator
        self.t = GoogleTranslator(source="ru", target="kk")

    def _call(self, text):
        for attempt in range(8):
            try:
                result = self.t.translate(text)
                time.sleep(1.5)  # stay well under Google's rate limit
                return result or text
            except Exception as e:
                wait = min(30 * (attempt + 1), 180)
                print(f"  request failed, waiting {wait}s and retrying... ({str(e)[:80]})")
                time.sleep(wait)
        raise RuntimeError("Google kept refusing. Wait 15-30 minutes and run the same command again; "
                           "finished paragraphs are remembered.")

    def translate(self, chunks):
        return [self._call(c) for c in chunks]

    def translate_group(self, texts):
        """Translate several short paragraphs in ONE request (joined by newlines).
        Falls back to one-by-one if Google merges or splits lines."""
        result = self._call("\n".join(texts)).split("\n")
        result = [r.strip() for r in result if r.strip()]
        if len(result) == len(texts):
            return result
        return [self._call(t) for t in texts]


class NLLBBackend:
    max_len = 400
    # Use "facebook/nllb-200-distilled-1.3B" for better quality if you have ~8 GB RAM free
    model_name = "facebook/nllb-200-distilled-600M"

    def __init__(self):
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"Loading {self.model_name} on {self.device} (first run downloads the model)...")
        self.tok = AutoTokenizer.from_pretrained(self.model_name, src_lang="rus_Cyrl")
        self.model = AutoModelForSeq2SeqLM.from_pretrained(self.model_name).to(self.device).eval()
        self.tgt_id = self.tok.convert_tokens_to_ids("kaz_Cyrl")

    def translate(self, chunks, batch_size=8):
        out = []
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            enc = self.tok(batch, return_tensors="pt", padding=True,
                           truncation=True, max_length=512).to(self.device)
            with self.torch.no_grad():
                gen = self.model.generate(**enc, forced_bos_token_id=self.tgt_id,
                                          max_new_tokens=512, num_beams=4)
            out.extend(self.tok.batch_decode(gen, skip_special_tokens=True))
        return out


# ---------- walking the document ----------

def paragraphs_in(container):
    yield from container.paragraphs
    for table in container.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from paragraphs_in(cell)


def all_paragraphs(doc):
    yield from paragraphs_in(doc)
    for section in doc.sections:
        for part in (section.header, section.footer):
            if not part.is_linked_to_previous:
                yield from paragraphs_in(part)


def set_text(p, text):
    """Put the translation into the paragraph's first text run and empty the others.
    Keeps paragraph style, the first run's formatting, and any images."""
    runs = [r for r in p._p.xpath("./w:r | ./w:hyperlink/w:r") if r.xpath("./w:t")]
    if not runs:
        return
    first_ts = runs[0].xpath("./w:t")
    first_ts[0].text = text
    first_ts[0].set(qn("xml:space"), "preserve")
    for t in first_ts[1:]:
        runs[0].remove(t)
    for r in runs[1:]:
        for t in r.xpath("./w:t"):
            r.remove(t)


# ---------- main ----------

def main():
    args = sys.argv[1:]
    offline = "--offline" in args
    args = [a for a in args if a != "--offline"]
    if len(args) != 2:
        print(__doc__)
        sys.exit(1)
    src_path, dst_path = args

    backend = NLLBBackend() if offline else GoogleBackend()
    doc = Document(src_path)

    paras, seen = [], set()
    for p in all_paragraphs(doc):
        if p._p in seen:  # merged table cells repeat the same paragraph
            continue
        seen.add(p._p)
        if CYRILLIC.search(p.text):  # skip empty lines, numbers, etc.
            paras.append(p)

    # Progress file: finished translations survive a crash or a block, so a rerun continues.
    cache_path = dst_path + ".progress.json"
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
        print(f"Resuming: {len(cache)} paragraphs already translated earlier")

    def save_cache():
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)

    todo = []
    for p in paras:
        text = " ".join(p.text.split())  # normalise line breaks / extra spaces
        if text not in cache and text not in todo:
            todo.append(text)
    print(f"{len(paras)} paragraphs in the document, {len(todo)} left to translate")

    # Group short paragraphs into batches to send far fewer requests
    groups, cur, size = [], [], 0
    for text in todo:
        if len(text) > backend.max_len or not hasattr(backend, "translate_group"):
            groups.append([text])
            continue
        if cur and size + len(text) + 1 > backend.max_len:
            groups.append(cur)
            cur, size = [], 0
        cur.append(text)
        size += len(text) + 1
    if cur:
        groups.append(cur)

    done = 0
    for g in groups:
        if len(g) == 1:
            cache[g[0]] = " ".join(backend.translate(split_chunks(g[0], backend.max_len)))
        else:
            for src, dst in zip(g, backend.translate_group(g)):
                cache[src] = dst
        done += len(g)
        save_cache()
        print(f"  {done}/{len(todo)} translated")

    for p in paras:
        set_text(p, cache[" ".join(p.text.split())])
    doc.save(dst_path)
    os.remove(cache_path)
    print(f"Saved: {dst_path}")


if __name__ == "__main__":
    main()