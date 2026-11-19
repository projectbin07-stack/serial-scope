# Contributing

Bug reports, new telemetry formats and pull requests are welcome.

## Development setup

```console
git clone https://github.com/projectbin07-stack/serial-scope.git
cd serial-scope
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip
```

## Checks

All four must pass; CI runs them on Ubuntu (under Xvfb) and Windows with Python 3.10 and 3.13.

```console
ruff check .
ruff format --check .
mypy
pytest --cov
```

The Tk smoke test (`tests/test_gui_smoke.py`) is skipped when no display is available. On
Linux, run it with `xvfb-run -a pytest`.

## Guidelines

- Runtime dependencies are pyserial and the standard library only.
- Keep logic out of `gui.py`. Anything that can be decided without Tk belongs in `app.py`,
  `plotmodel.py` or `terminal.py`, where it is unit-tested.
- A new decoder needs tests that feed its fixture stream in several chunk sizes (down to one
  byte), with garbage between records and corrupted checksums, and that check the error
  counters. Errors are counted and reported, never dropped silently.
- If a format comes from another project, copy its reference vectors into `tests/data/` and
  record where they came from in `tests/data/SOURCES.md`.
- Anything that sends bytes to a device (like `commands.py`) must be checked byte for byte
  against that project's own vectors, and anything that can move a motor must have a stop
  path and must not be described as tested on hardware unless it was.
- Do not describe anything as tested on hardware unless you ran it on hardware; say which
  board, adapter and OS.
- Keep commits small with imperative messages (`Add ...`, `Fix ...`).
