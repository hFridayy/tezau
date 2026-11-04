"""The `tezau` command.

  tezau translate rusfile.docx kazfile.docx --providers gemini,claude,openai --vote-terms
  tezau estimate rusfile.docx
  tezau terms list
  tezau terms set "договор" "шарт"
  tezau check-keys

Run `tezau <command> --help` for all options.
"""
import argparse
import json
import logging
import os
import sys

from dotenv import find_dotenv, load_dotenv

from .docx_io import iter_paragraphs, needs_translation, normalize, open_document
from .engine import CONTEXT_CHARS, make_pages, translate_document
from .glossary import Glossary, extract_terms, vote_terms
from .memory import Store
from .providers import PROVIDERS, AuthError, ProviderError, get_provider

log = logging.getLogger("tezau")


# ------------------------------------------------------------------ helpers

def parse_pairs(items: list, what: str) -> dict:
    """['claude=claude-haiku-5-5', 'openai=gpt-5-mini'] -> {'claude': ..., 'openai': ...}"""
    pairs = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not value:
            sys.exit(f"--{what} expects name=value, got {item!r}")
        pairs[key.strip()] = value.strip()
    return pairs


def make_providers(names: str, models: dict) -> list:
    providers = []
    for name in [n.strip() for n in names.split(",") if n.strip()]:
        if name == "fake":
            from .fake import FakeProvider
            providers.append(FakeProvider(model=models.get("fake")))
            continue
        try:
            providers.append(get_provider(name, models.get(name)))
        except (AuthError, ValueError) as e:
            print(f"  skipping {name}: {e}")
        except ImportError:
            print(f"  skipping {name}: its library is not installed (pip install -e \".[{name}]\")")
    if not providers:
        sys.exit("No provider is available. Check your .env file and the --providers list.")
    return providers


def document_texts(path: str) -> list:
    return [normalize(p.text) for p in iter_paragraphs(open_document(path)) if needs_translation(p.text)]


def load_prices(path: str = "prices.json") -> dict:
    """Optional prices.json: {"model-name": [usd per 1M input tokens, usd per 1M output tokens]}"""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def cost(usage, price) -> float:
    return (usage.input_tokens * price[0] + usage.output_tokens * price[1]) / 1e6


# ------------------------------------------------------------------ commands

def cmd_translate(args) -> int:
    if not os.path.exists(args.input):
        sys.exit(f"File not found: {args.input}")
    providers = make_providers(args.providers, parse_pairs(args.model, "model"))
    dry_run = any(p.name == "fake" for p in providers)
    if dry_run:
        print("  dry run with the fake provider: the memory database is not used")
    store = None if (args.no_memory or dry_run) else Store(args.db)
    # the glossary lives in the same database; --no-memory only switches off the translation memory
    terms_db = None if dry_run else (store or (Store(args.db) if os.path.exists(args.db) else None))

    glossary = None
    if terms_db and args.vote_terms:
        texts = document_texts(args.input)
        terms = extract_terms(texts, max_terms=args.max_terms, min_count=args.min_count)
        known = terms_db.terms()
        new_terms = [t for t in terms if args.revote or t not in known]
        new_terms = [t for t in new_terms if known.get(t, ("", ""))[1] != "human"]  # never re-vote yours
        print(f"Key terms: {len(terms)} found, {len(new_terms)} new - voting with {len(providers)} model(s)...")
        weights = {k: float(v) for k, v in parse_pairs(args.weight, "weight").items()}
        for d in vote_terms(new_terms, providers, weights=weights, store=terms_db):
            flag = "  <- needs a human decision" if d["status"] == "disputed" else ""
            print(f"  {d['source']} -> {d['target']}  [{d['status']}]{flag}")
    if terms_db and not args.no_glossary:
        glossary = Glossary.from_store(terms_db)

    def progress(page, provider, done, total):
        print(f"  page {page:>3} by {provider:<7} ({done}/{total} done)", flush=True)

    print(f"Translating {args.input} with {', '.join(map(repr, providers))} ...")
    try:
        r = translate_document(args.input, args.output, providers, workers=args.workers,
                               block_pages=args.block_pages, page_chars=args.page_chars, notes=args.notes,
                               store=store, glossary=glossary, max_pages=args.max_pages,
                               on_page_done=progress)
    except KeyboardInterrupt:
        print("\nStopped. Finished pages are saved - run the same command again to continue.")
        return 130
    except ProviderError as e:
        print(f"\nStopped: {e}")
        return 1

    print(f"\nSaved {r.output}: {r.paragraphs} paragraphs, {r.pages} pages translated, "
          f"{r.from_memory} paragraphs reused from memory, {r.seconds:.0f}s")
    if r.failed:
        for name, message in r.failed.items():
            print(f"  {name} dropped out: {message[:150]}")
    prices, total = load_prices(), 0.0
    for p in providers:
        u = p.usage
        if not u.requests:
            continue
        line = (f"  {p!r}: {r.pages_by_provider.get(p.name, 0)} pages, {u.requests} requests, "
                f"tokens in/out {u.input_tokens}/{u.output_tokens} (thinking {u.reasoning_tokens})")
        if p.model in prices:
            total += cost(u, prices[p.model])
            line += f", ${cost(u, prices[p.model]):.4f}"
        print(line)
    if total:
        print(f"  total cost: ${total:.4f}")
    return 0


def cmd_estimate(args) -> int:
    """Rough size and cost before spending anything. Cyrillic text is about 3 characters per token."""
    texts = list(dict.fromkeys(document_texts(args.input)))
    cached = Store(args.db).tm_get_many(texts) if os.path.exists(args.db) else {}
    todo = [t for t in texts if t not in cached]
    pages = make_pages(todo, args.page_chars)
    chars = sum(len(t) for t in todo)
    tokens_in = chars / 3 + len(pages) * (450 + CONTEXT_CHARS / 3)  # text + instructions + context per page
    tokens_out = chars / 2.5                                        # Kazakh needs a few more tokens
    print(f"{args.input}: {len(texts)} unique paragraphs, {len(cached)} already in memory")
    print(f"To translate: {chars:,} characters -> {len(pages)} pages (requests)")
    print(f"Estimated tokens: ~{tokens_in:,.0f} in, ~{tokens_out:,.0f} out (without thinking tokens)")
    prices = load_prices()
    for model, (p_in, p_out) in prices.items():
        print(f"  {model}: ~${(tokens_in * p_in + tokens_out * p_out) / 1e6:.3f}")
    if not prices:
        print('To see costs, create prices.json from your providers\' pricing pages: '
              '{"model-name": [USD per 1M input tokens, USD per 1M output tokens]}')
    return 0


def cmd_terms(args) -> int:
    store = Store(args.db)
    if args.action == "set":
        store.term_put(args.source, args.target, status="human")
        print(f"Saved: {args.source} -> {args.target} (human, models will not change it)")
        return 0
    rows = [r for r in store.term_rows() if not args.status or r["status"] == args.status]
    if not rows:
        print("No terms yet. Run translate with --vote-terms first.")
    for r in rows:
        votes = ", ".join(f"{k}: {v}" for k, v in r["votes"].items())
        print(f"  [{r['status']:8}] {r['source']} -> {r['target']}" + (f"   ({votes})" if votes else ""))
    return 0


def cmd_eval(args) -> int:
    try:
        from . import evaluation
        import sacrebleu  # noqa: F401  (needed for the metrics)
    except ImportError:
        sys.exit('Evaluation needs sacrebleu: pip install -e ".[eval]"')
    if args.eval_command == "check":
        judge = make_providers(args.judge, parse_pairs(args.model, "model"))[0] if args.judge else None
        store = Store(args.db) if os.path.exists(args.db) else None
        print(evaluation.run_check(args.source, args.translation, judge, args.sample, store=store))
    elif args.eval_command == "post-edit":
        if args.save_to_memory and not args.source:
            sys.exit("--save-to-memory needs --source with the original Russian file")
        store = Store(args.db) if args.save_to_memory else None
        print(evaluation.run_post_edit(args.machine, args.edited, args.pages, args.minutes, args.source, store))
    elif args.eval_command == "consistency":
        if not os.path.exists(args.db):
            sys.exit(f"{args.db} not found - consistency needs the terms from a translate --vote-terms run")
        print(evaluation.run_consistency(args.source, args.translation, Store(args.db), args.pages))
    return 0


def cmd_check_keys(args) -> int:
    providers = make_providers(args.providers, parse_pairs(args.model, "model"))
    failures = 0
    for p in providers:
        p.max_attempts = 1
        try:
            text = p.complete("Translate from Russian to Kazakh. Reply with the translation only.",
                              "Договор вступает в силу с момента подписания.", json_mode=False)
            print(f"  OK    {p!r} (thinking={p.reasoning}): {text.strip()}  | {p.usage}")
        except ProviderError as e:
            failures += 1
            print(f"  FAIL  {p!r}: {str(e)[:200]}")
    return 1 if failures else 0


# ------------------------------------------------------------------ argument parsing

def build_parser() -> argparse.ArgumentParser:
    all_names = ",".join(PROVIDERS)
    ap = argparse.ArgumentParser(prog="tezau",
                                 description="Translate Word documents from Russian to Kazakh with LLMs.")
    ap.add_argument("-v", "--verbose", action="store_true", help="show detailed logs")
    sub = ap.add_subparsers(dest="command", required=True)

    def provider_options(p, default):
        p.add_argument("--providers", default=default,
                       help=f"comma-separated: {all_names}, or fake for a free dry run (default: {default})")
        p.add_argument("--model", action="append", metavar="PROVIDER=MODEL",
                       help="choose a model, e.g. --model openai=gpt-5-mini (repeatable)")

    p = sub.add_parser("translate", help="translate a .docx file")
    p.add_argument("input")
    p.add_argument("output")
    provider_options(p, "gemini")
    p.add_argument("--workers", type=int, default=1, help="parallel requests per provider (default 1)")
    p.add_argument("--block-pages", type=int, default=5, help="pages per work block (default 5)")
    p.add_argument("--page-chars", type=int, default=2500, help="characters per page (default 2500)")
    p.add_argument("--notes", default="", help='extra instructions, e.g. "formal style"')
    p.add_argument("--max-pages", type=int, help="translate only the first N pages (cheap tests)")
    p.add_argument("--vote-terms", action="store_true", help="let the models vote on key terms first")
    p.add_argument("--max-terms", type=int, default=60)
    p.add_argument("--revote", action="store_true", help="vote again on terms already in the database")
    p.add_argument("--min-count", type=int, default=3, help="how often a term must appear (default 3)")
    p.add_argument("--weight", action="append", metavar="PROVIDER=NUMBER",
                   help="vote weight, e.g. --weight claude=1.5 (default 1 each)")
    p.add_argument("--db", default="tezau.db", help="memory database file")
    p.add_argument("--no-memory", action="store_true", help="don't reuse or save translations (the glossary is still used)")
    p.add_argument("--no-glossary", action="store_true", help="don't give the models the agreed terms (for experiments)")
    p.set_defaults(func=cmd_translate)

    p = sub.add_parser("estimate", help="size and rough cost before translating")
    p.add_argument("input")
    p.add_argument("--page-chars", type=int, default=2500)
    p.add_argument("--db", default="tezau.db")
    p.set_defaults(func=cmd_estimate)

    p = sub.add_parser("terms", help="show or correct the shared glossary")
    terms = p.add_subparsers(dest="action", required=True)
    t = terms.add_parser("list", help="show terms")
    t.add_argument("--status", choices=["agreed", "single", "disputed", "human"])
    t.add_argument("--db", default="tezau.db")
    t = terms.add_parser("set", help="set a term yourself (models will never change it)")
    t.add_argument("source")
    t.add_argument("target")
    t.add_argument("--db", default="tezau.db")
    p.set_defaults(func=cmd_terms)

    p = sub.add_parser("eval", help="measure translation quality")
    ev = p.add_subparsers(dest="eval_command", required=True)
    e = ev.add_parser("check", help="automatic checks on every paragraph (+ optional AI judge)")
    e.add_argument("source", help="the original Russian .docx")
    e.add_argument("translation", help="the translated .docx")
    e.add_argument("--judge", metavar="PROVIDER", help="AI judge, e.g. claude - best a different model than the translator")
    e.add_argument("--model", action="append", metavar="PROVIDER=MODEL")
    e.add_argument("--sample", type=int, default=30, help="paragraphs for the AI judge (default 30)")
    e.add_argument("--db", default="tezau.db", help="memory database, for problems per model")
    e = ev.add_parser("post-edit", help="how much the human editor changed")
    e.add_argument("machine", help="the machine translation, unchanged")
    e.add_argument("edited", help="the same file after your corrections")
    e.add_argument("--pages", type=float, help="compare only the first N pages (the part you reviewed)")
    e.add_argument("--minutes", type=float, help="how long the editing took, for minutes per page")
    e.add_argument("--source", help="the original Russian .docx (needed for --save-to-memory)")
    e.add_argument("--save-to-memory", action="store_true", help="save your edits to memory as human translations")
    e.add_argument("--db", default="tezau.db")
    e = ev.add_parser("consistency", help="how often the agreed terms were used, per model")
    e.add_argument("source")
    e.add_argument("translation")
    e.add_argument("--pages", type=float, help="measure only the first N pages")
    e.add_argument("--db", default="tezau.db")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("check-keys", help="one tiny test request per provider")
    provider_options(p, all_names)
    p.set_defaults(func=cmd_check_keys)
    return ap


def main(argv=None) -> int:
    load_dotenv(find_dotenv(usecwd=True))  # the .env in the folder you run the command from
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "google_genai", "anthropic", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
