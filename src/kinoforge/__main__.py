"""Entry point — ``python -m kinoforge`` delegates to the argparse CLI."""

from kinoforge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
