"""Theke -- self-hosted media manager CLI.

scaffolding: a single module for now, to be split per pipeline stage
later. For the moment it only proves the toolchain runs end to end.
"""


def greeting() -> str:
    """Return the placeholder greeting (real stages replace this later)."""
    return "Hallo Welt"


def main() -> None:
    """CLI entry point."""
    print(greeting())


if __name__ == "__main__":
    main()
