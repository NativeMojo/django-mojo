import mistune
from pygments import highlight
from pygments.lexers import get_lexer_by_name
from pygments.styles import get_style_by_name
from pygments.formatters import HtmlFormatter
from pygments.util import ClassNotFound


class HighlightRenderer(mistune.HTMLRenderer):
    def block_code(self, code, info=None):
        if not info:
            return f'\n<pre>{mistune.escape(code)}</pre>\n'
        try:
            lexer = get_lexer_by_name(info, stripall=True)
        except ClassNotFound:
            return f'\n<pre>{mistune.escape(code)}</pre>\n'
        formatter = HtmlFormatter(
            linenos=False,
            cssclass="highlight",
            style=get_style_by_name("monokai")
        )
        return highlight(code, lexer, formatter)

class MarkdownRenderer:
    _renderer = None

    def __init__(self):
        if not self._renderer:
            self._initialize_renderer()

    def _initialize_renderer(self):
        # plugins = self._discover_plugins()
        self._renderer = mistune.create_markdown(
            renderer=HighlightRenderer(escape=False),
            escape=False,
            hard_wrap=True,
            plugins=[]
        )

    def _discover_plugins(self):
        # from mojo.apps.docit.markdown_plugins import syntax_highlight
        plugins = [
            'table', 'url', 'task_list',
            'footnotes', 'abbr', 'mark', 'math']
        return plugins

    def render(self, markdown_text):
        return self._renderer(markdown_text)
