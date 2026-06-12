"""Smoke tests for the phase 1 scaffolding."""

from theke.theke import greeting, main


def test_greeting_returns_hallo_welt():
    assert greeting() == "Hallo Welt"


def test_main_prints_greeting(capsys):
    main()
    assert capsys.readouterr().out.strip() == "Hallo Welt"
