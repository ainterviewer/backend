"""HTML-to-PDF rendering shared by every document export.

Every PDF the app produces is a Jinja template rendered to HTML and laid out
by WeasyPrint, so documents share one styling system: `base.html.j2` sets up
the page, the bundled fonts and the theme, and a document template extends it
with its own content. The theme is a set of CSS custom properties, which is
where per-project styling (logo, colours, fonts) is meant to plug in later.

WeasyPrint runs no JavaScript. Anything drawn client-side, like the analysis
charts, has to reach a template as finished SVG.
"""

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from weasyprint import HTML

TEMPLATES_DIR = Path(__file__).parent / "templates"
#: Relative URLs in the templates resolve here, which is how `@font-face`
#: finds the bundled fonts without a network fetch.
ASSETS_DIR = Path(__file__).parents[2] / "assets"

document_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["j2"]),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_html(template: str, context: dict[str, Any]) -> str:
    return document_env.get_template(template).render(**context)


def render_pdf(template: str, context: dict[str, Any]) -> bytes:
    html = render_html(template, context)
    return HTML(string=html, base_url=str(ASSETS_DIR)).write_pdf()
