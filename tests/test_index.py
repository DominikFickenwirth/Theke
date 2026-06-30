"""Tests for the library indexer (phase 12): the pure parsing/walking helpers
in theke.index plus the scan orchestration (resolve/upsert/sweep + safety)."""

import json
import os

import pytest

from theke.index import parse_folder_title, nfo_tmdb_id, is_lang_variant


# -- folder-name parser ------------------------------------------------------

def test_parse_folder_title_plain():
    assert parse_folder_title("Die Klapperschlange (1981)") == ("Die Klapperschlange", 1981)


def test_parse_folder_title_trailing_junk():
    # quality/edition suffixes after the year are tolerated; year still wins.
    assert parse_folder_title("Mein Film (2020) [1080p]") == ("Mein Film", 2020)


def test_parse_folder_title_no_year_is_none():
    assert parse_folder_title("No Year Here") is None


# -- nfo uniqueid parser -----------------------------------------------------

def test_nfo_tmdb_id_uniqueid():
    assert nfo_tmdb_id('<movie><uniqueid type="tmdb">603</uniqueid></movie>') == "603"


def test_nfo_tmdb_id_attr_order_and_default():
    # type may sit after other attributes; single quotes accepted.
    assert nfo_tmdb_id("<uniqueid default='true' type='tmdb'>77</uniqueid>") == "77"


def test_nfo_tmdb_id_legacy_tmdbid():
    assert nfo_tmdb_id("<movie><tmdbid>551</tmdbid></movie>") == "551"


def test_nfo_tmdb_id_none_when_only_imdb():
    assert nfo_tmdb_id('<movie><uniqueid type="imdb">tt001</uniqueid></movie>') is None


# -- language-variant filename -----------------------------------------------

def test_is_lang_variant_true():
    assert is_lang_variant("Mein Film (2020).en.mp4") is True


def test_is_lang_variant_false_for_primary():
    assert is_lang_variant("Mein Film (2020).mp4") is False
