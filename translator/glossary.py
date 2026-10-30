"""Shared terminology: find key terms, let the models vote on their translations,
and give every page the agreed terms that appear on it.

Why vote on terms and not on whole paragraphs? Translating every paragraph three
times would triple the cost. Voting on ~60 key terms costs a few small requests,
yet makes all models use the same words for the things that matter most.
"""
import json
import logging
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from .providers import ProviderError

log = logging.getLogger(__name__)

WORD = re.compile(r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)?")

# Common Russian words that are never terms on their own (prepositions, pronouns, fillers).
STOPWORDS = set("""
а без более бы был была были было быть в вам вас весь во вот все всего всех вы где да даже для до его
ее ей если есть еще же за и из или им их к как ко когда кто ли либо между мне может мы на над
не него нее нет ни них но о об однако он она они оно от по под при про с со так также такой там
те тем то того тоже той только том ты у уже хотя чем через что чтобы эта эти это этого этой этом
этот является являются которые который которая которое которых которым настоящего настоящий
настоящей данный данной данного иные иных случае случаях соответствии согласно течение также
должен должна должно должны после перед каждый каждая каждое каждого каждой каждом один одна одно
одного одной одном отдельно автоматически соответствующий соответствующая соответствующие
соответствующими соответствующих необходимо возможность возможно можно нужно должна иметь
быть будет будут были время раз всегда сразу далее ниже выше например любой любого других другой
""".split())


# Russian noun and adjective endings, longest first.
ENDINGS = sorted("""иями иях ией ием иям ами ями ого его ому ему ыми ими ых их ии ах ях ов ев ей ий ый ой
ая яя ое ее ые ие ую юю ом ем ам ям ия ию а я о е ы и у ю ь""".split(), key=len, reverse=True)


def stem(word: str) -> str:
    """A rough Russian stem: drop the case ending, so 'договор', 'договора', 'договору' -> 'договор'."""
    w = word.lower().replace("ё", "е")
    for ending in ENDINGS:
        if w.endswith(ending) and len(w) - len(ending) >= 3:
            return w[:-len(ending)]
    return w


# Typical Russian verb endings: terms are nouns and noun phrases, so phrases with verbs are skipped.
VERB_ENDINGS = ("ть", "ться", "тся", "ется", "ются", "ает", "яет", "ует", "ают", "яют", "уют")


def is_verb_like(word: str) -> bool:
    return len(word) > 4 and word.endswith(VERB_ENDINGS)


def extract_terms(paragraphs: list, max_terms: int = 60, min_count: int = 3) -> list:
    """Find repeated words and 1-3 word phrases - local and free, no API calls.

    Different forms of the same phrase ('договор поставки', 'договора поставки') are
    counted together by their stems; the most frequent form represents them.
    """
    counts = Counter()
    forms = defaultdict(Counter)
    for paragraph in paragraphs:
        words = [w.lower() for w in WORD.findall(paragraph)]
        for n in (1, 2, 3):
            for i in range(len(words) - n + 1):
                gram = words[i:i + n]
                if (gram[0] in STOPWORDS or gram[-1] in STOPWORDS
                        or any(len(w) < 3 or is_verb_like(w) for w in gram)):
                    continue
                key = tuple(stem(w) for w in gram)
                counts[key] += 1
                forms[key][" ".join(gram)] += 1

    ranked = sorted((k for k, c in counts.items() if c >= min_count), key=lambda k: (-counts[k], -len(k)))
    chosen = []
    for key in ranked:
        # Skip a phrase that (almost) only appears inside a longer chosen phrase.
        inside = any(len(c) > len(key) and _contains(c, key) and counts[c] >= 0.95 * counts[key] for c in chosen)
        if not inside:
            chosen.append(key)
        if len(chosen) == max_terms:
            break
    return [forms[k].most_common(1)[0][0] for k in chosen]


def _contains(longer: tuple, shorter: tuple) -> bool:
    n = len(shorter)
    return any(longer[i:i + n] == shorter for i in range(len(longer) - n + 1))


# ------------------------------------------------------------------ voting

TERMS_PROMPT = """You are a terminologist preparing a glossary for translating a {source} document into {target}.
For each {source} term you receive, give the standard {target} equivalent used in formal and official documents,
in its dictionary (base) form.
Return ONLY a JSON object whose keys are exactly the given terms and whose values are the translations."""


# Latin letters that look exactly like Cyrillic ones. Models sometimes mix them into a
# Cyrillic word ("ережe" with a Latin e): it looks right but breaks search and comparison.
LOOKALIKES = str.maketrans({"a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у", "i": "і", "h": "һ", "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І", "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х"})
CYRILLIC_WORD = re.compile(r"\w*[А-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі]\w*")
KAZAKH_PLURAL = re.compile(r"(?<=\w{3})(лар|лер|дар|дер|тар|тер)\b")


def fix_lookalikes(text: str) -> str:
    """Replace Latin look-alike letters, but only inside words that contain Cyrillic."""
    return CYRILLIC_WORD.sub(lambda m: m.group(0).translate(LOOKALIKES), text)


def _clean(text: str) -> str:
    return fix_lookalikes(re.sub(r"\s+", " ", str(text)).strip().strip(".,;:!?\"'«»()"))


def _norm(text: str) -> str:
    """Comparison key for votes: lower case, ё = е, and the Kazakh plural ending removed,
    so 'ұпайлар' and 'ұпай' count as the same answer."""
    return KAZAKH_PLURAL.sub("", _clean(text).lower().replace("ё", "е"))


def vote_terms(terms: list, providers: list, *, weights: dict = None, store=None,
               source: str = "Russian", target: str = "Kazakh (Cyrillic script)", batch: int = 40) -> list:
    """Every provider proposes a translation for every term; the weighted majority wins.

    weights: {"claude": 2.0, ...} - e.g. from benchmark scores; default 1.0 each.
    Returns a list of decisions and saves them to the store, if one is given.
    """
    from .engine import parse_json_object  # local import avoids a circular import
    weights = weights or {}
    system = TERMS_PROMPT.format(source=source, target=target)

    def ask(provider):
        answers = {}
        for i in range(0, len(terms), batch):
            chunk = terms[i:i + batch]
            try:
                data = parse_json_object(provider.complete(system, json.dumps(chunk, ensure_ascii=False)))
            except (ProviderError, ValueError) as e:
                log.warning("%r could not vote on terms: %s", provider, e)
                break
            for term in chunk:
                if _clean(data.get(term, "")):
                    answers[term] = _clean(data[term])
        return provider.name, answers

    with ThreadPoolExecutor(max_workers=max(1, len(providers))) as pool:  # all models vote at once
        ballots = dict(pool.map(ask, providers))

    decisions = []
    for term in terms:
        votes = {name: answers[term] for name, answers in ballots.items() if term in answers}
        if not votes:
            continue
        score, voters, original = defaultdict(float), defaultdict(int), {}
        for name, candidate in votes.items():
            key = _norm(candidate)
            score[key] += weights.get(name, 1.0)
            voters[key] += 1
            if key not in original or len(candidate) < len(original[key]):
                original[key] = candidate  # keep the shortest form: usually the dictionary form
        winner = max(score, key=lambda k: (score[k], voters[k]))
        agreement = score[winner] / sum(score.values())
        if len(votes) == 1:
            status = "single"
        elif voters[winner] >= 2 and agreement > 0.5:
            status = "agreed"
        else:
            status = "disputed"  # no majority: the human editor should decide
        decision = {"source": term, "target": original[winner], "votes": votes,
                    "agreement": round(agreement, 2), "status": status}
        decisions.append(decision)
        if store:
            store.term_put(term, original[winner], votes, round(agreement, 2), status)
    return decisions


# ------------------------------------------------------------------ using the glossary

class Glossary:
    """Agreed terms; for_text() returns only the terms that appear in a given text,
    so each request carries a few relevant lines, not the whole glossary."""

    def __init__(self, entries: dict):
        self.entries = dict(entries)  # {source term: target}
        self._stems = {term: [stem(w) for w in WORD.findall(term)] for term in self.entries}

    @classmethod
    def from_store(cls, store) -> "Glossary":
        return cls({source: target for source, (target, _status) in store.terms().items()})

    def __len__(self) -> int:
        return len(self.entries)

    def for_text(self, text: str) -> list:
        """Terms whose words appear in the text with the same stems, in the same order.
        Stems must be EQUAL: a prefix match let 'поля' (stem 'пол') match 'пользователь'."""
        stems_in_text = [stem(w) for w in WORD.findall(text)]
        found = []
        for term, stems in self._stems.items():
            n = len(stems)
            if n and any(stems_in_text[i:i + n] == stems for i in range(len(stems_in_text) - n + 1)):
                found.append((term, self.entries[term]))
        return found
