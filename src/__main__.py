"""Entry point for the zipapp bundle and for `python3 src`.

Lives beside the package rather than inside it: zipapp places the contents of
this directory at the archive root, so the package must be importable by name
from here.
"""

import sys

from n8n_warden.cli import run

sys.exit(run())
