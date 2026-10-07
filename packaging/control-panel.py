# Entry point for the PyInstaller build (control-panel.exe). PyInstaller runs this
# as a plain script, so it can't use the package's relative-import __main__.py.
from control_panel.app import main

raise SystemExit(main())
