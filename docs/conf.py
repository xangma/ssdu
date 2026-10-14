"""Configuration copied into the allowlisted publication staging tree."""

from ssdu import __version__

project = "SSDU"
author = "SSDU contributors"
release = __version__
extensions = ["myst_parser", "sphinx.ext.autodoc", "sphinx.ext.napoleon"]
source_suffix = {".md": "markdown"}
root_doc = "index"
exclude_patterns = []
html_theme = "furo"
html_title = "SSDU documentation"
html_show_sourcelink = False
html_copy_source = False
html_show_sphinx = False
html_theme_options = {"navigation_with_keys": True}
html_static_path = ["_static"]
html_favicon = "_static/favicon.svg"
myst_enable_extensions = ["colon_fence"]
myst_heading_anchors = 3
autodoc_typehints = "none"
autodoc_member_order = "bysource"
nitpicky = True
linkcheck_retries = 1
linkcheck_timeout = 10
linkcheck_workers = 2
