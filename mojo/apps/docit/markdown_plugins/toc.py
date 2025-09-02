def plugin_toc(md):
    """
    A mistune v3 plugin to support a table of contents placeholder [TOC].
    This uses a render hook to perform a simple text substitution.
    """
    def before_render_hook(renderer, text, state):
        # Simple text replacement for the [TOC] placeholder
        return text.replace('[TOC]', '<div class="toc"></div>')

    md.before_render_hooks.append(before_render_hook)

plugin = plugin_toc
