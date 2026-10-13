"""Message text as styled spans, for transcripts.

Interviewer messages come from the interview guide and may carry a little
inline markup -- a bolded instruction, a link to a support page. The platform
renders it through an allowlist (`frontend/src/lib/utils/sanitize.ts`), and a
transcript should read the same, not show `<u>` as text. This is the same
allowlist: listed tags become marks, anything else stays visible as written,
and `<script>`/`<style>` go with their content.

The result is a list of spans rather than HTML, because Word needs it too:
the PDF template turns spans into tags, python-docx into runs.

Participant messages are never parsed: what a respondent typed is text.
"""

import re
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser

from markupsafe import Markup

# Tag -> the mark it carries. Synonyms share one, as they render alike.
_MARKS = {
    "b": "bold",
    "strong": "bold",
    "i": "italic",
    "em": "italic",
    "u": "underline",
    "s": "strike",
    "code": "code",
    "a": "link",
}
_OPAQUE = {"script", "style"}
_SAFE_SCHEMES = {"http", "https", "mailto", "tel"}
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")

# Control tokens the interviewer emits, shown as what they mean.
TOKEN_LABELS = {
    "<|endofprobe|>": "Probe complete",
    "<|endofsection|>": "Section complete",
    "<|endofinterview|>": "Interview complete",
    "<|skipquestion|>": "Question skipped",
    "<|skipsection|>": "Section skipped",
    "<|noanswer|>": "No answer",
    "<|restartinterview|>": "Interview restarted",
}
_TOKEN = re.compile("|".join(re.escape(token) for token in TOKEN_LABELS))

_HTML_TAGS = {
    "bold": "b",
    "italic": "i",
    "underline": "u",
    "strike": "s",
    "code": "code",
}


@dataclass(frozen=True)
class Span:
    text: str
    marks: frozenset[str] = frozenset()
    # A control token, `text` being its label.
    token: bool = False


def _safe_href(href: str) -> bool:
    href = href.strip()
    scheme = _SCHEME.match(href)
    if scheme is None:
        return bool(href)
    return scheme[1].lower() in _SAFE_SCHEMES


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.spans: list[Span] = []
        # Open allowed elements, innermost last: (tag, mark).
        self.open: list[tuple[str, str | None]] = []
        self.opaque = 0

    def _emit(self, text: str) -> None:
        if not text or self.opaque:
            return
        marks = frozenset(mark for _, mark in self.open if mark)
        self.spans.append(Span(text, marks))

    def handle_starttag(self, tag, attrs):
        if tag in _OPAQUE:
            self.opaque += 1
        elif tag == "br":
            self._emit("\n")
        elif tag == "a":
            # A link looks like one but goes nowhere: a transcript is a record
            # of what was shown, not a way off to wherever the guide pointed.
            # One to nowhere safe was never a link, and keeps only its label.
            safe = _safe_href(dict(attrs).get("href") or "")
            self.open.append((tag, "link" if safe else None))
        elif tag in _MARKS:
            self.open.append((tag, _MARKS[tag]))
        else:
            self._emit(self.get_starttag_text() or "")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self._emit("\n")
        elif tag not in _MARKS and tag not in _OPAQUE:
            self._emit(self.get_starttag_text() or "")

    def handle_endtag(self, tag):
        if tag in _OPAQUE:
            self.opaque = max(0, self.opaque - 1)
        elif tag in _MARKS:
            # Close the innermost match; a stray closing tag is dropped.
            for index in range(len(self.open) - 1, -1, -1):
                if self.open[index][0] == tag:
                    del self.open[index]
                    break
        elif tag != "br":
            self._emit(f"</{tag}>")

    def handle_data(self, data):
        self._emit(data)


def _markup(text: str) -> list[Span]:
    parser = _Parser()
    parser.feed(text)
    parser.close()
    return parser.spans


def parse(text: str, *, markup: bool) -> list[Span]:
    """`text` as spans; guide markup is honoured only when `markup` is set.

    Tokens are split out first: to an HTML parser `<|endofprobe|>` is a `<`
    and some text, which it may hand over in pieces.
    """
    spans: list[Span] = []
    cursor = 0
    for chunk_end, token in [
        *((match.start(), match) for match in _TOKEN.finditer(text)),
        (len(text), None),
    ]:
        chunk = text[cursor:chunk_end]
        if chunk:
            spans += _markup(chunk) if markup and "<" in chunk else [Span(chunk)]
        if token is not None:
            spans.append(Span(TOKEN_LABELS[token[0]], token=True))
            cursor = token.end()
    return spans


def only_token(spans: list[Span]) -> str | None:
    """The token's label, when a message is nothing but one."""
    content = [span for span in spans if span.token or span.text.strip()]
    if len(content) == 1 and content[0].token:
        return content[0].text
    return None


def plain(spans: list[Span]) -> str:
    return "".join(f"[{span.text}]" if span.token else span.text for span in spans)


def to_html(spans: list[Span]) -> Markup:
    parts: list[str] = []
    for span in spans:
        if span.token:
            parts.append(f'<span class="token">{escape(span.text)}</span>')
            continue
        html = escape(span.text)
        for mark in sorted(span.marks):
            if tag := _HTML_TAGS.get(mark):
                html = f"<{tag}>{html}</{tag}>"
        if "link" in span.marks:
            html = f'<span class="link">{html}</span>'
        parts.append(html)
    return Markup("".join(parts))
