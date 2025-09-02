from pygments import highlight
from pygments.lexers import get_lexer_by_name
from pygments.formatters import HtmlFormatter
from mistune.util import escape_html

def _render_fenced_code_with_highlight(renderer, token, state):
    lang = token['attrs'].get('lang')
    code = token['text']

    if not lang:
        return f'<pre><code>{escape_html(code)}</code></pre>\n'
    try:
        lexer = get_lexer_by_name(lang, stripall=True)
        formatter = HtmlFormatter()
        return highlight(code, lexer, formatter)
    except Exception:
        return f'<pre><code class="language-{lang}">{escape_html(code)}</code></pre>\n'

def plugin_highlight(md):
    """
    A mistune v3 plugin for syntax highlighting of fenced code blocks.
    """
    md.renderer.register('fenced_code', _render_fenced_code_with_highlight)

plugin = plugin_highlight
