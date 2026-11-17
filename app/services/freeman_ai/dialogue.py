"""Shared customer conversation context and explicit operator requests."""
import json
import re

_DIRECT_HANDOFF = re.compile(
    r"(?:оператор(?:а)?|живой человек|human|manager|менеджер(?:а)?"
    r"|(?:пожалуйста,? )?(?:позовите|подключите|пригласите) (?:оператора|человека|менеджера)(?:,? пожалуйста)?"
    r"|(?:хочу|можно) (?:поговорить|связаться) с (?:оператором|человеком|менеджером))"
)
_WORD = re.compile(r"[a-zа-яё0-9]+", re.I)
_STOP = {"что", "как", "мне", "для", "это", "вот", "или", "меня", "есть"}


def explicit_handoff(text):
    phrase = " ".join(text.casefold().split()).strip(" .!?")
    return bool(_DIRECT_HANDOFF.fullmatch(phrase))


def relevant_articles(articles, query):
    terms = {word for word in _WORD.findall(query.casefold()) if len(word) > 2} - _STOP

    def score(article):
        title = set(_WORD.findall(article.title.casefold()))
        body = set(_WORD.findall(article.text.casefold()))
        return 3 * len(terms & title) + len(terms & body)

    return sorted(articles, key=score, reverse=True)


def fit_context(facts, turns, *, limit):
    """Keep trusted reference data and the current turn, trim oldest turns first."""
    def size(items):
        return len(json.dumps(items, ensure_ascii=False, default=str).encode())

    # Images are separately bounded by the attachment pipeline and budgeted
    # using their inference cost instead of the transport base64 size.
    def priced(item):
        content = item.get("content")
        if not isinstance(content, list):
            return item
        return {**item, "content": [
            {"type": "input_text", "text": "I" * 4096}
            if part.get("type") == "input_image" else part
            for part in content
        ]}

    selected = []
    used = size(facts)
    for turn in reversed(turns):
        cost = size(priced(turn)) + 2
        if selected and used + cost > limit:
            break
        selected.append(turn)
        used += cost
    return [*facts, *reversed(selected)]
