"""Plain-text -> HTML body rendering (email_format.render_html_body).

This is the formatting the cold send adds so a message lands as a real
email instead of an unstyled text/plain blob. The rules it locks in:
paragraph structure from blank lines, <br> from single newlines, HTML
escaping of untrusted copy, and linkified URLs/emails. The stored body
stays plain text — this only shapes the HTML alternative at send time.
"""
from __future__ import annotations

from app.email_format import render_html_body


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
