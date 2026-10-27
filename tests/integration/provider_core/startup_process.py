"""Invoke the real idle foreground process without native or recovery substitutes."""

from nmrnet_provider.process import main


if __name__ == "__main__":
    raise SystemExit(main())
