"""Plain-text -> HTML body rendering (email_format.render_html_body).

This is the formatting the cold send adds so a message lands as a real
email instead of an unstyled text/plain blob. The rules it locks in:
paragraph structure from blank lines, <br> from single newlines, HTML
escaping of untrusted copy, and linkified URLs/emails. The stored body
stays plain text — this only shapes the HTML alternative at send time.
"""
from __future__ import annotations

from app.email_format import anchor_to_text, join_signature, render_html_body


def test_blank_lines_become_separate_paragraphs():
    html = render_html_body("Hi Jordan,\n\nQuick question about your book.\n\nRushel")
    assert html.count("<p ") == 3
    assert "Hi Jordan," in html
    assert "Quick question about your book." in html
    assert "Rushel" in html


def test_single_newline_becomes_a_line_break_not_a_new_paragraph():
    html = render_html_body("Line one\nLine two")
    assert html.count("<p ") == 1        # one paragraph...
    assert "<br>" in html                # ...with a line break inside


def test_html_special_characters_are_escaped():
    """The body is untrusted copy — a literal < or & must never reach the
    recipient as markup."""
    html = render_html_body("Cover A & B <not a tag>")
    assert "&amp;" in html
    assert "&lt;not a tag&gt;" in html
    assert "<not a tag>" not in html


def test_a_bare_url_is_linkified():
    html = render_html_body("See https://example.com/plans for details.")
    assert '<a href="https://example.com/plans"' in html
    # the trailing sentence period stays outside the link
    assert "/plans</a>" in html


def test_a_www_host_gets_an_https_href():
    html = render_html_body("Visit www.example.com today")
    assert '<a href="https://www.example.com"' in html
    assert ">www.example.com</a>" in html


def test_an_email_address_becomes_a_mailto_link():
    html = render_html_body("Reply to rushel@renegade.com anytime")
    assert '<a href="mailto:rushel@renegade.com"' in html


def test_a_url_query_string_ampersand_is_escaped_in_the_href():
    """Escaping runs before linkify, so a & in the query survives as &amp;
    in both the text and the href (valid HTML attribute encoding)."""
    html = render_html_body("Book at https://x.com/a?b=1&c=2 now")
    assert "b=1&amp;c=2" in html
    assert 'href="https://x.com/a?b=1&amp;c=2"' in html


def test_empty_or_blank_body_renders_nothing():
    # The caller (mailjet.send) omits the HTML part entirely on "".
    assert render_html_body("") == ""
    assert render_html_body("   \n\n  ") == ""
    assert render_html_body(None) == ""


def test_carriage_returns_are_normalised():
    """A body drafted with CRLF must paragraph-split the same as LF."""
    html = render_html_body("Para one\r\n\r\nPara two")
    assert html.count("<p ") == 2


def test_output_is_inline_styled_not_a_style_block():
    """Gmail/Outlook strip <style>/<head>; the wrapper must carry inline
    styles so the formatting actually survives."""
    html = render_html_body("Body")
    assert "<style" not in html
    assert html.startswith("<div style=")


# ---- safe inline anchor (the drafter embeds the calculator link) ------


def test_a_safe_https_anchor_is_preserved_not_escaped():
    """The drafter embeds a real <a href="https://…"> in the body; render keeps
    it as a live, styled link (with the custom anchor text) rather than escaping
    it to visible tag text."""
    body = ('Curious what your book is worth? '
            '<a href="https://renegadeinsurance.com/agency-value-calculator/'
            '?utm_source=automation">take a quick look here</a>.')
    html = render_html_body(body)
    assert ('href="https://renegadeinsurance.com/agency-value-calculator/'
            '?utm_source=automation"') in html
    assert ">take a quick look here</a>" in html
    assert "&lt;a" not in html            # NOT escaped into literal tag text


def test_a_safe_anchor_href_ampersand_is_escaped_in_the_attribute():
    html = render_html_body('<a href="https://x.com/c?a=1&b=2">go</a>')
    assert 'href="https://x.com/c?a=1&amp;b=2"' in html   # & -> &amp; in the attr
    assert ">go</a>" in html


def test_a_non_https_anchor_is_escaped_not_passed_through():
    """Only https anchors pass; an http one is treated as ordinary (untrusted)
    copy and escaped — the original tag never becomes live markup."""
    html = render_html_body('<a href="http://evil.example">x</a>')
    assert "&lt;a href=" in html


def test_an_anchor_with_extra_attributes_is_not_passed_through():
    """A strictly href-only shape passes; anything with more (e.g. onclick) is
    escaped, so a script attribute can never reach the recipient as markup."""
    html = render_html_body('<a href="https://x.com" onclick="alert(1)">x</a>')
    assert 'onclick="alert(1)">' not in html   # never a live attribute
    assert "&lt;a href=" in html               # the original tag is escaped


def test_anchor_to_text_flattens_to_text_and_url():
    out = anchor_to_text('See <a href="https://x.com/calc">this page</a> now')
    assert out == "See this page (https://x.com/calc) now"


def test_anchor_to_text_leaves_plain_text_untouched():
    assert anchor_to_text("no link here") == "no link here"
    assert anchor_to_text(None) is None


# ---- signature (bold block in the HTML part; joined for the text part) ----


def test_join_signature_appends_with_a_blank_line():
    assert join_signature("Body.", "Best,\nAayush") == "Body.\n\nBest,\nAayush"


def test_join_signature_is_a_noop_without_a_signature():
    assert join_signature("Body.", None) == "Body."
    assert join_signature("Body.", "   ") == "Body."


def test_signature_renders_as_a_bold_block_with_line_breaks():
    """The signature is its own final paragraph, WHOLE BLOCK bold
    (operator-directed), newlines as <br> — visually set apart from the copy."""
    html = render_html_body("Body para.",
                            signature="Best,\nAayush Gupta\n+1 678 500 9991")
    assert 'font-weight:bold' in html
    sig_block = html[html.index("font-weight:bold"):]
    assert "Best,<br>" in sig_block
    assert "Aayush Gupta<br>" in sig_block
    assert "+1 678 500 9991" in sig_block
    # The body paragraph itself is NOT bold — only the signature block is.
    body_part = html[:html.index("font-weight:bold")]
    assert "Body para." in body_part


def test_signature_is_escaped_not_markup():
    """The signature is operator config but still escaped — a < in it must
    never reach the recipient as live markup."""
    html = render_html_body("Body", signature="Best <M&A>")
    assert "Best &lt;M&amp;A&gt;" in html


def test_no_signature_adds_no_bold_block():
    html = render_html_body("Body")
    assert "font-weight:bold" not in html


def test_empty_body_renders_nothing_even_with_a_signature():
    # No body -> no HTML part at all; a signature alone is never sent.
    assert render_html_body("", signature="Best,\nA") == ""
