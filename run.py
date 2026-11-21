"""PyInstaller entry point (relative imports don't work as a script).
No arguments -> launch the GUI (double-click friendly)."""
import sys

from ytmp_dl.cli import main

sys.exit(main() if len(sys.argv) > 1 else main(["gui"]))
