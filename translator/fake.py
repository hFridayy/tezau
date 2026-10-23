"""A fake provider for free dry runs and tests: no API, no key, no cost.

It "translates" by putting [kk] in front of every paragraph, but it speaks the same
JSON format as the real models, so the whole pipeline runs exactly as it would for real.
"""
import json

from .providers import Provider


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, model=None, **kwargs):
        kwargs.setdefault("api_key", "not-needed")
        super().__init__(model=model or "echo", **kwargs)

    def _call(self, system, user, json_mode):
        if system.startswith("You are a terminologist"):  # term voting
            answer = {term: f"[kk] {term}" for term in json.loads(user)}
        elif "Translate this page:\n" in user:            # a page of paragraphs
            page = json.loads(user.split("Translate this page:\n", 1)[1])
            answer = {key: f"[kk] {text}" for key, text in page.items()}
        else:                                             # plain text fallback
            return f"[kk] {user}", len(user) // 3, len(user) // 3
        text = json.dumps(answer, ensure_ascii=False)
        return text, len(user) // 3, len(text) // 3
