"""Render a plain-text email body to a simple, email-safe HTML part.

The drafter and the review UI keep the body as PLAIN TEXT — one editable
source of truth. At send time we ALSO produce an HTML part so the message
lands as a properly formatted email (paragraphs with spacing, line breaks,
clickable links, a readable font) instead of an unstyled text/plain blob.
The plain text still rides along as the multipart text alternative, so
text-only clients are unaffected and nothing about the stored copy changes.

Deliberately NOT a markdown renderer: cold first-touch copy is plain prose
by design (the drafting prompt forbids hype and visual formatting), so all
this does is structure the existing whitespace and linkify — it never injects
styling the copy did not ask for. The output is inline-styled (no <style>
block or <head>): Gmail and Outlook strip or ignore those, inline survives.
"""
from __future__ import annotations

import html
import re

# One or more blank lines separate paragraphs; a single newline is a line
# break within a paragraph.
_PARA_SPLIT = re.compile(r"\n[ \t]*\n+")

# A bare URL, a www. host, or an email address. Matched against ALREADY
# HTML-escaped text, so the character class tolerates the entities escaping
# introduces (e.g. the & in a query string is &amp; by this point).
_LINK_RE = re.compile(
    r"(?P<url>(?:https?://|www\.)[^\s<]+)"
    r"|(?P<email>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)",
    re.IGNORECASE,
)

# Sentence punctuation that trails a link ("...visit example.com.") belongs
# to the prose, not the href — peel it off and leave it outside the anchor.
_TRAILING = ".,;:!?)]}>\"'"

# Inline styles only (see module docstring). A neutral system font stack, a
# comfortable line height, and per-paragraph bottom margin for the spacing
# that a text/plain part cannot express.
_WRAPPER = (
    "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
    "Roboto,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.6;"
    'color:#222222;">{body}</div>'
)
_PARAGRAPH = '<p style="margin:0 0 16px;">{content}</p>'
_LINK = '<a href="{href}" style="color:#2563eb;text-decoration:underline;">{text}</a>'


def _linkify(escaped: str) -> str:
    """Wrap URLs/emails in the escaped text with anchors. Operates on escaped
    text so the anchors it adds are the only markup in the result."""
    def repl(match: re.Match) -> str:
        token = match.group(0)
        trail = ""
        while token and token[-1] in _TRAILING:
            trail = token[-1] + trail
            token = token[:-1]
        if not token:
            return match.group(0)
        if match.lastgroup == "email":
            href = f"mailto:{token}"
        elif token.lower().startswith("http"):
            href = token
        else:
            href = f"https://{token}"   # www.x.com -> a real, clickable href
        return _LINK.format(href=href, text=token) + trail

    return _LINK_RE.sub(repl, escaped)


def render_html_body(text: str | None) -> str:
    """A plain-text body -> a minimal inline-styled HTML document, or ``""``
    when the body is empty (the caller then omits the HTML part rather than
    sending an empty one)."""
    if not text or not text.strip():
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs = []
    for block in _PARA_SPLIT.split(normalized):
        block = block.strip("\n")
        if not block.strip():
            continue
        # Escape FIRST (the body is untrusted copy), then linkify and turn
        # the remaining single newlines into <br> — so neither step can be
        # neutralised by escaping, and the copy can never inject markup.
        escaped = html.escape(block, quote=False)
        linked = _linkify(escaped)
        paragraphs.append(_PARAGRAPH.format(content=linked.replace("\n", "<br>\n")))
    if not paragraphs:
        return ""
    return _WRAPPER.format(body="".join(paragraphs))
