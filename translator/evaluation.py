"""Evaluation: how good is the translation, how much did the human editor change,
and did the models really use the agreed terms?

  check        automatic checks on every paragraph (+ optional AI judge on a sample)
  post-edit    compare the machine output with the human-edited version (HTER, % changed)
  consistency  how often each agreed glossary term was actually used, per model

All three compare documents paragraph by paragraph. The translated document keeps the
structure of the original, so paragraph N of one file matches paragraph N of the other.
"""
import difflib
import json
import os
import random
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime

from .docx_io import iter_paragraphs, needs_translation, normalize, open_document
from .engine import looks_untranslated, parse_json_object
from .glossary import KAZAKH_PLURAL, Glossary

RESULTS_DIR = "results"
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# a word that mixes Cyrillic and Latin letters, like "ережe" with a Latin "e"
MIXED_SCRIPT = re.compile(r"\b(?=\w*[А-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі])(?=\w*[A-Za-z])\w+\b")


# ------------------------------------------------------------------ shared helpers

def doc_paragraphs(path: str) -> list:
    """All paragraphs (body, tables, headers, footers) as normalized text, in document order."""
    return [normalize(p.text) for p in iter_paragraphs(open_document(path))]


def align(a: list, b: list) -> list:
    """Pair paragraphs of two versions of a document: [(index_in_a, index_in_b)].
    Same length -> by position. Otherwise an editor added or removed paragraphs,
    so match the unchanged stretches with difflib and pair what lies between."""
    if len(a) == len(b):
        return list(zip(range(len(a)), range(len(b))))
    pairs = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            pairs.extend(zip(range(i1, i2), range(j1, j2)))
    return pairs


def save_report(name: str, data: dict, markdown: list) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    base = f"{RESULTS_DIR}/{name}_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(markdown) + "\n")
    return base + ".md"


def short(text: str, n: int = 160) -> str:
    return text if len(text) <= n else text[:n] + "…"


# ------------------------------------------------------------------ metrics

def chrf(hyps: list, refs: list) -> float:
    """chrF++: character n-gram F-score (0-100). Works well for languages with many word endings."""
    import sacrebleu
    return sacrebleu.corpus_chrf(hyps, [refs], word_order=2).score


def ter(hyps: list, refs: list) -> float:
    """Translation Edit Rate (%): edits needed to turn hyps into refs, per reference word.
    Against a human-edited version it is called HTER - the editor's workload."""
    import sacrebleu
    return sacrebleu.corpus_ter(hyps, [refs]).score


def bootstrap_ci(hyps: list, refs: list, metric, rounds: int = 1000, seed: int = 0) -> tuple:
    """95% confidence interval by resampling paragraphs: is a difference real or noise?"""
    rng, n, scores = random.Random(seed), len(hyps), []
    for _ in range(rounds):
        idx = [rng.randrange(n) for _ in range(n)]
        scores.append(metric([hyps[i] for i in idx], [refs[i] for i in idx]))
    scores.sort()
    return round(scores[int(0.025 * rounds)], 2), round(scores[int(0.975 * rounds)], 2)


# ------------------------------------------------------------------ check

CHECKS = {
    "empty": "translation is missing",
    "untranslated": "still Russian (unchanged, or a long text without any Kazakh-only letter)",
    "mixed_script": "a word mixes Latin and Cyrillic letters (e.g. a Latin 'e' inside a Kazakh word)",
    "numbers_changed": "numbers differ from the source (fine if a number was written out in words)",
    "length_suspicious": "translation is under half or over double the source length",
}

JUDGE_PROMPT = """You are an expert Russian-Kazakh translation reviewer.
You receive a JSON list of items with "id", "source" (Russian) and "translation" (Kazakh).
For each item return "accuracy" 1-5 (5 = meaning fully kept), "fluency" 1-5 (5 = natural Kazakh) and
"errors": a list of {"type": mistranslation|omission|addition|grammar|terminology|untranslated|other,
"severity": minor|major|critical, "span": the problematic Kazakh words}.
Be strict and specific. Return ONLY a JSON object: {"results": [{"id": ..., "accuracy": ..., "fluency": ..., "errors": [...]}]}"""

MQM_WEIGHTS = {"minor": 1, "major": 5, "critical": 10}  # standard MQM severity weights


def judge(provider, items: list, batch: int = 8) -> dict:
    """AI judge with MQM-style error annotation. Use a different provider than the translator:
    models tend to rate their own output higher (self-preference bias)."""
    results = {}
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        try:
            data = parse_json_object(provider.complete(JUDGE_PROMPT, json.dumps(chunk, ensure_ascii=False)))
            for r in data.get("results", []):
                results[str(r.get("id"))] = r
        except ValueError:
            continue
    scored = [results[str(it["id"])] for it in items if str(it["id"]) in results]
    if not scored:
        return {"judged": 0}
    errors = [e for r in scored for e in r.get("errors", []) if isinstance(e, dict)]
    words = sum(len(it["source"].split()) for it in items if str(it["id"]) in results)
    penalty = sum(MQM_WEIGHTS.get(e.get("severity"), 1) for e in errors)
    return {
        "judge": repr(provider), "judged": len(scored),
        "accuracy": round(statistics.mean(float(r.get("accuracy", 0)) for r in scored), 2),
        "fluency": round(statistics.mean(float(r.get("fluency", 0)) for r in scored), 2),
        "mqm_per_100_words": round(100 * penalty / max(words, 1), 2),
        "errors_by_type": dict(Counter(e.get("type", "other") for e in errors)),
        "serious": [(r.get("id"), e) for r in scored for e in r.get("errors", [])
                    if isinstance(e, dict) and e.get("severity") in ("major", "critical")],
    }


def run_check(source: str, translation: str, judge_provider=None, sample: int = 30, seed: int = 42) -> str:
    src, tgt = doc_paragraphs(source), doc_paragraphs(translation)
    pairs = [(i, src[i], tgt[j]) for i, j in align(src, tgt) if needs_translation(src[i])]
    flags = defaultdict(list)
    for i, s, t in pairs:
        if not t:
            flags["empty"].append(i)
            continue
        if looks_untranslated(s, t):
            flags["untranslated"].append(i)
        if MIXED_SCRIPT.search(t):
            flags["mixed_script"].append(i)
        if sorted(NUMBER.findall(s)) != sorted(NUMBER.findall(t)):
            flags["numbers_changed"].append(i)
        if len(s) > 30 and not 0.5 <= len(t) / len(s) <= 2.0:
            flags["length_suspicious"].append(i)
    flagged = sorted({i for v in flags.values() for i in v})
    by_index = {i: (s, t) for i, s, t in pairs}
    passed = 100 * (len(pairs) - len(flagged)) / max(len(pairs), 1)

    md = [f"# Translation check: {translation}", "",
          f"- Paragraphs checked: **{len(pairs)}**",
          f"- Passed every automatic check: **{passed:.1f}%**", "",
          "| Check | Paragraphs | Meaning |", "|---|---|---|"]
    md += [f"| {k} | {len(flags[k])} | {v} |" for k, v in CHECKS.items()]
    report = {"source": source, "translation": translation, "checked": len(pairs),
              "pass_rate": round(passed, 1), "flags": {k: len(flags[k]) for k in CHECKS}}

    if judge_provider:
        candidates = [(i, s, t) for i, s, t in pairs if len(s) > 40 and t]
        chosen = random.Random(seed).sample(candidates, min(sample, len(candidates)))
        j = judge(judge_provider, [{"id": i, "source": s, "translation": t} for i, s, t in chosen])
        report["judge"] = {k: v for k, v in j.items() if k != "serious"}
        if j.get("judged"):
            md += ["", "## AI judge", "",
                   f"- {j['judge']} on {j['judged']} random paragraphs (use a different model than the translator)",
                   f"- Accuracy **{j['accuracy']}/5**, fluency **{j['fluency']}/5**",
                   f"- MQM error penalty **{j['mqm_per_100_words']}** per 100 words (lower is better)",
                   f"- Errors by type: {j['errors_by_type']}", ""]
            md += [f"- #{pid} {e.get('severity')} {e.get('type')}: {e.get('span', '')}" for pid, e in j["serious"]]

    md += ["", "## Flagged paragraphs (first 40) - start your review here", ""]
    for i in flagged[:40]:
        s, t = by_index[i]
        reasons = ", ".join(k for k in CHECKS if i in flags[k])
        md += [f"**#{i}** ({reasons})", f"- RU: {short(s)}", f"- KK: {short(t)}", ""]
    path = save_report("check", report, md)
    return "\n".join(md[:7 + len(CHECKS)]) + (
        f"\n\nAI judge: accuracy {report['judge']['accuracy']}/5, fluency {report['judge']['fluency']}/5, "
        f"MQM {report['judge']['mqm_per_100_words']} per 100 words" if report.get("judge", {}).get("judged") else ""
    ) + f"\n\nFull report: {path}"


# ------------------------------------------------------------------ post-edit

def first_pages_cutoff(paragraphs: list, pages: float, page_chars: int = 2500) -> int:
    """Index of the first paragraph after the first `pages` pages (counted in characters)."""
    total = 0
    for i, text in enumerate(paragraphs):
        total += len(text)
        if total > pages * page_chars:
            return i + 1
    return len(paragraphs)


def run_post_edit(machine: str, edited: str, pages: float = None, minutes: float = None,
                  source: str = None, store=None) -> str:
    """How much did the human change? Optionally save the edited paragraphs to memory as human edits.
    pages: compare only the first N pages - the part you actually reviewed."""
    mt, pe = doc_paragraphs(machine), doc_paragraphs(edited)
    cutoff = first_pages_cutoff(mt, pages) if pages else len(mt)
    pairs = [(i, j) for i, j in align(mt, pe) if mt[i] and pe[j] and i < cutoff]
    if not pairs:
        return "No paragraphs to compare - check the file names and --pages."
    hyps, refs = [mt[i] for i, _ in pairs], [pe[j] for _, j in pairs]
    changed = [(i, j) for i, j in pairs if mt[i] != pe[j]]
    import sacrebleu
    per_paragraph = sorted(((sacrebleu.sentence_ter(mt[i], [pe[j]]).score, i, j) for i, j in changed), reverse=True)
    hter = ter(hyps, refs)
    chars = sum(len(r) for r in refs)
    pages = chars / 2500

    md = [f"# Post-editing effort: {machine} -> {edited}", "",
          f"- Paragraphs compared: **{len(pairs)}** (about {pages:.1f} pages of 2,500 characters)",
          f"- Paragraphs the editor changed: **{len(changed)}** ({100 * len(changed) / len(pairs):.1f}%)",
          f"- HTER (edits per 100 reference words, lower is better): **{hter:.1f}**",
          f"- chrF++ of the machine output against the edited version: **{chrf(hyps, refs):.1f}**"]
    report = {"machine": machine, "edited": edited, "paragraphs": len(pairs), "changed": len(changed),
              "changed_percent": round(100 * len(changed) / len(pairs), 1), "hter": round(hter, 2),
              "chrf": round(chrf(hyps, refs), 2), "pages": round(pages, 2)}
    if minutes:
        md.append(f"- Editing time: **{minutes:.0f} min**, about **{minutes / max(pages, 0.1):.1f} min per page**")
        report["minutes"], report["minutes_per_page"] = minutes, round(minutes / max(pages, 0.1), 2)
    md += ["", "## Most edited paragraphs", ""]
    for score, i, j in per_paragraph[:15]:
        md += [f"**#{i}** (TER {score:.0f})", f"- machine: {short(mt[i])}", f"- edited:  {short(pe[j])}", ""]

    if store and source:  # the editor's version becomes the trusted translation
        src = doc_paragraphs(source)
        known = store.tm_providers([src[i] for i, _ in pairs if i < len(src)])
        saved = []
        for i, j in pairs:
            if i < len(src) and needs_translation(src[i]):
                # changed by the editor -> written by "human"; confirmed unchanged -> keeps the model's name
                who = "human" if mt[i] != pe[j] else known.get(src[i], ("", "human"))[1]
                saved.append((src[i], pe[j], who))
        store.tm_put_many(saved, provider="human", human=True)  # all of them are now human-approved
        md.append(f"Saved {len(saved)} reviewed paragraphs to memory as human translations.")
        report["saved_to_memory"] = len(saved)
    path = save_report("post_edit", report, md)
    return "\n".join(md[:7 + (1 if minutes else 0)]) + f"\n\nFull report: {path}"


# ------------------------------------------------------------------ consistency

def _kazakh_roots(term: str) -> list:
    """Roots of a Kazakh term, robust to endings: drop the plural, and the last letter of
    longer words (it can change before a suffix: кітап -> кітабы, тақ -> тағы)."""
    words = KAZAKH_PLURAL.sub("", term.lower()).split()
    return [w[:-1] if len(w) >= 5 else w for w in words]


def run_consistency(source: str, translation: str, store) -> str:
    """For each agreed term: in how many paragraphs that contain it was the agreed translation used?"""
    terms = {s: t for s, (t, status) in store.terms().items() if status in ("agreed", "human", "single")}
    if not terms:
        return "No agreed terms in the database. Run translate with --vote-terms first."
    glossary = Glossary(terms)
    src, tgt = doc_paragraphs(source), doc_paragraphs(translation)
    providers = store.tm_providers([src[i] for i, _ in align(src, tgt)])
    per_term = defaultdict(lambda: [0, 0])           # term -> [used, occurrences]
    per_provider = defaultdict(lambda: [0, 0])
    for i, j in align(src, tgt):
        if not needs_translation(src[i]):
            continue
        words = tgt[j].lower().split()
        for term, target in glossary.for_text(src[i]):
            used = all(any(w.startswith(root) for w in words) for root in _kazakh_roots(target))
            per_term[term][0] += used
            per_term[term][1] += 1
            target_in_memory, who = providers.get(src[i], ("", "not in memory"))
            if normalize(target_in_memory) != tgt[j]:  # memory changed since (e.g. human edits saved)
                who = "not in memory"
            per_provider[who][0] += used
            per_provider[who][1] += 1
    total_used = sum(u for u, _ in per_term.values())
    total = sum(n for _, n in per_term.values())
    md = [f"# Term consistency: {translation}", "",
          f"- Agreed terms: **{len(terms)}**, found in the source **{total}** times",
          f"- Agreed translation used: **{100 * total_used / max(total, 1):.1f}%**", "",
          "| Model | Term occurrences | Agreed term used |", "|---|---|---|"]
    md += [f"| {who} | {n} | {100 * u / n:.1f}% |" for who, (u, n) in sorted(per_provider.items())]
    md += ["", "## Least consistent terms", "", "| Term | Agreed translation | Used | Occurrences |", "|---|---|---|---|"]
    worst = sorted(per_term.items(), key=lambda kv: kv[1][0] / kv[1][1])
    md += [f"| {t} | {terms[t]} | {100 * u / n:.0f}% | {n} |" for t, (u, n) in worst[:20]]
    report = {"terms": len(terms), "occurrences": total, "used_percent": round(100 * total_used / max(total, 1), 1),
              "per_provider": {k: {"used": u, "occurrences": n} for k, (u, n) in per_provider.items()},
              "per_term": {k: {"used": u, "occurrences": n} for k, (u, n) in per_term.items()}}
    path = save_report("consistency", report, md)
    return "\n".join(md[:8 + len(per_provider)]) + f"\n\nFull report: {path}"
