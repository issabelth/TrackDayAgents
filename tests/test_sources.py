import pytest

from trackday.sources import SourceError, load_sources, make_source
from trackday.storage.db import EventRepository


def test_make_source_web():
    s = make_source("https://jcgroup.com.pl/oferta/")
    assert s.kind == "web" and s.enabled
    assert s.name == "jcgroup.com.pl" and s.base_url == "https://jcgroup.com.pl/oferta/"
    assert make_source("https://jcgroup.com.pl/oferta/").id == s.id  # stały identyfikator


@pytest.mark.parametrize("url", ["", "jcgroup.com.pl", "ftp://x.pl/", "javascript:alert(1)"])
def test_make_source_rejects_bad_urls(url):
    with pytest.raises(SourceError):
        make_source(url)


def test_sources_are_seeded_once_and_persist():
    repo = EventRepository(":memory:")
    defaults = load_sources()
    repo.init_sources(defaults)
    assert [s.id for s in repo.list_sources()] == [s.id for s in defaults]

    own = make_source("https://jcgroup.com.pl/oferta/", organizer="JC Group")
    repo.save_sources(repo.list_sources() + [own])
    repo.init_sources(defaults)                      # kolejne uruchomienie nie nadpisuje listy
    assert repo.list_sources()[-1].organizer == "JC Group"

    repo.save_sources([])                            # usunięcie wszystkich też jest zapamiętane
    repo.init_sources(defaults)
    assert repo.list_sources() == []


def test_clearing_events_keeps_sources():
    repo = EventRepository(":memory:")
    repo.save_sources([make_source("https://jcgroup.com.pl/oferta/")])
    repo.clear()
    assert len(repo.list_sources()) == 1
