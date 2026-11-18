"""Allow ``python -m serial_scope``."""

from serial_scope.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
