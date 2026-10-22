# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""CBZ downloads embed the library's metadata into ComicInfo.xml.

``calibredb export`` only writes ComicBookInfo into the zip comment, which
comic readers ignore. Downloads (web, OPDS and Kobo all reach
``do_download_file``) now merge the library's metadata into the archive's
ComicInfo.xml: a filled library field wins, an empty one keeps the XML's
value, and everything the library does not model is preserved. When the XML
already matches, the original file is served without a copy.
"""

import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree
from werkzeug.datastructures import Headers

from cps.services import comicinfo_embed

pytestmark = pytest.mark.unit

_EXISTING = b"""<?xml version="1.0" encoding="utf-8"?>
<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Title>Old Title</Title>
  <Series>Old Series</Series>
  <Number>1</Number>
  <Summary>File summary</Summary>
  <Writer>File Writer</Writer>
  <Penciller>File Penciller</Penciller>
  <Publisher>File Publisher</Publisher>
  <Manga>YesAndRightToLeft</Manga>
  <Pages>
    <Page Image="0" Type="FrontCover"/>
    <Page Image="1" DoublePage="true"/>
  </Pages>
  <CustomExtension>kept</CustomExtension>
</ComicInfo>
"""


def _book(**overrides):
    values = dict(
        id=42,
        path="book",
        title="Library Title",
        series=[SimpleNamespace(name="Library Series")],
        series_index=3.0,
        comments=[SimpleNamespace(text="<p>Library <b>summary</b></p>")],
        pubdate=datetime(2021, 5, 9, tzinfo=timezone.utc),
        authors=[SimpleNamespace(name="Alice Author"), SimpleNamespace(name="Bob Author")],
        publishers=[],
        tags=[SimpleNamespace(name="Action"), SimpleNamespace(name="Noir")],
        languages=[SimpleNamespace(lang_code="eng")],
        ratings=[SimpleNamespace(rating=9)],
        identifiers=[SimpleNamespace(type="isbn", val="9781234567897")],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _empty_book(**overrides):
    values = dict(title="", series=[], series_index=1.0, comments=[],
                  pubdate=datetime(101, 1, 1), authors=[SimpleNamespace(name="Unknown")],
                  publishers=[], tags=[], languages=[], ratings=[], identifiers=[])
    values.update(overrides)
    return _book(**values)


def _text(root, name):
    element = root.find(name)
    return None if element is None else element.text


def _merged(existing, book):
    merged = comicinfo_embed.merge_comicinfo(existing, comicinfo_embed.library_fields(book))
    return None if merged is None else etree.fromstring(merged)


def test_filled_library_fields_replace_the_xml_values():
    root = _merged(_EXISTING, _book())
    assert _text(root, "Title") == "Library Title"
    assert _text(root, "Series") == "Library Series"
    assert _text(root, "Number") == "3"
    assert _text(root, "Summary") == "Library summary"
    assert _text(root, "Writer") == "Alice Author, Bob Author"
    assert (_text(root, "Year"), _text(root, "Month"), _text(root, "Day")) == ("2021", "5", "9")
    assert _text(root, "Tags") == "Action, Noir"
    assert _text(root, "LanguageISO") == "en"
    assert _text(root, "CommunityRating") == "4.5"
    assert _text(root, "GTIN") == "9781234567897"


def test_empty_library_fields_keep_the_xml_values():
    root = _merged(_EXISTING, _book(publishers=[], comments=[], series=[]))
    assert _text(root, "Publisher") == "File Publisher"
    assert _text(root, "Summary") == "File summary"
    assert _text(root, "Series") == "Old Series"
    assert _text(root, "Number") == "1"


def test_placeholder_author_does_not_overwrite_the_writer():
    root = _merged(_EXISTING, _empty_book(title="Library Title"))
    assert _text(root, "Writer") == "File Writer"


def test_fields_the_library_does_not_model_are_preserved():
    root = _merged(_EXISTING, _book())
    assert _text(root, "Penciller") == "File Penciller"
    assert _text(root, "Manga") == "YesAndRightToLeft"
    assert _text(root, "CustomExtension") == "kept"
    pages = root.find("Pages")
    assert [dict(page.attrib) for page in pages] == [
        {"Image": "0", "Type": "FrontCover"},
        {"Image": "1", "DoublePage": "true"},
    ]
    assert root.nsmap["xsi"] == "http://www.w3.org/2001/XMLSchema-instance"


def test_new_elements_are_inserted_in_schema_order():
    root = _merged(_EXISTING, _book())
    names = [element.tag for element in root if isinstance(element.tag, str)]
    known = [name for name in names if name in comicinfo_embed._SCHEMA_INDEX]
    assert known == sorted(known, key=comicinfo_embed._SCHEMA_INDEX.__getitem__)


def test_matching_xml_needs_no_rewrite():
    matching = b"""<?xml version="1.0"?>
<ComicInfo>
  <Title>Library Title</Title>
  <Writer>Alice Author,Bob Author</Writer>
  <Number>3.0</Number>
  <Series>Library Series</Series>
  <Penciller>Only in the file</Penciller>
</ComicInfo>"""
    book = _empty_book(title="Library Title", series=[SimpleNamespace(name="Library Series")],
                       series_index=3,
                       authors=[SimpleNamespace(name="Alice Author"), SimpleNamespace(name="Bob Author")])
    assert _merged(matching, book) is None


def test_missing_xml_is_created_from_library_fields():
    root = _merged(None, _book())
    assert etree.QName(root).localname == "ComicInfo"
    assert _text(root, "Title") == "Library Title"


def test_missing_xml_and_empty_library_needs_no_rewrite():
    assert _merged(None, _empty_book()) is None


def test_unparseable_xml_is_replaced():
    root = _merged(b"<ComicInfo><Title>broken", _book())
    assert _text(root, "Title") == "Library Title"


def test_external_entities_are_not_resolved(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("leaked")
    hostile = (f'<?xml version="1.0"?><!DOCTYPE c [<!ENTITY x SYSTEM "file://{secret}">]>'
               '<ComicInfo><Notes>&x;</Notes></ComicInfo>').encode()
    merged = comicinfo_embed.merge_comicinfo(hostile, {"Title": "T"})
    assert b"leaked" not in merged


def _build_cbz(path, entries, comment=b""):
    with zipfile.ZipFile(path, "w") as archive:
        archive.comment = comment
        for name, (data, compression) in entries.items():
            archive.writestr(zipfile.ZipInfo(name, date_time=(2020, 1, 2, 3, 4, 6)), data,
                             compress_type=compression)


def test_rewrite_keeps_pages_and_comment_and_replaces_nested_comicinfo(tmp_path):
    source = tmp_path / "source.cbz"
    _build_cbz(source, {
        "Issue/page001.jpg": (b"\xff\xd8 page one", zipfile.ZIP_STORED),
        "Issue/page002.jpg": (b"\xff\xd8 page two" * 100, zipfile.ZIP_DEFLATED),
        "Issue/ComicInfo.xml": (_EXISTING, zipfile.ZIP_DEFLATED),
    }, comment=b'{"ComicBookInfo/1.0": {}}')
    target = tmp_path / "target.cbz"
    comicinfo_embed.write_cbz_with_comicinfo(str(source), str(target), b"<ComicInfo/>")

    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target) as dst:
        assert dst.comment == src.comment
        assert dst.namelist() == ["Issue/page001.jpg", "Issue/page002.jpg", "ComicInfo.xml"]
        assert dst.read("ComicInfo.xml") == b"<ComicInfo/>"
        for name in ("Issue/page001.jpg", "Issue/page002.jpg"):
            assert dst.read(name) == src.read(name)
            assert dst.getinfo(name).compress_type == src.getinfo(name).compress_type
            assert dst.getinfo(name).date_time == src.getinfo(name).date_time
        assert dst.testzip() is None


@pytest.fixture
def download_env(monkeypatch, tmp_path):
    from cps import helper
    from cps.progress_syncing import settings

    library = tmp_path / "library"
    book_dir = library / "book"
    book_dir.mkdir(parents=True)
    staged = tmp_path / "staged"
    staged.mkdir()
    config = SimpleNamespace(
        config_use_google_drive=False, config_embed_metadata=True,
        config_binariesdir="/calibre", config_kepubifypath="",
        get_book_path=lambda: str(library))
    monkeypatch.setattr(helper, "config", config)
    monkeypatch.setattr(helper, "get_temp_dir", lambda: str(staged))
    monkeypatch.setattr(comicinfo_embed, "get_temp_dir", lambda: str(staged))
    monkeypatch.setattr(helper, "current_user", SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr(settings, "is_koreader_sync_enabled", lambda: False)

    def no_calibre_export(*_args):
        raise AssertionError("CBZ downloads must not go through calibredb export")
    monkeypatch.setattr(helper, "do_calibre_export", no_calibre_export)

    served = []
    real_send = helper.send_from_directory

    def send(directory, name):
        served.append(Path(directory) / name)
        return real_send(directory, name)
    monkeypatch.setattr(helper, "send_from_directory", send)

    from cps import app
    state = SimpleNamespace(book=_book())

    def fetch():
        context = app.test_request_context("/download")
        with context:
            response = helper.do_download_file(
                state.book, "cbz", "", SimpleNamespace(name="comic"),
                Headers({"Content-Disposition": "attachment; filename=comic.cbz"}))
            response.direct_passthrough = False
            data = response.get_data()
            # Run only the after_this_request cleanup of the staged copy; the
            # app's own after_request hooks need a fully configured app.
            for callback in context._after_request_functions:
                response = callback(response)
            response.close()
            return response.status_code, data

    source = book_dir / "comic.cbz"
    _build_cbz(source, {
        "page001.jpg": (b"\xff\xd8 page", zipfile.ZIP_STORED),
        "ComicInfo.xml": (_EXISTING, zipfile.ZIP_DEFLATED),
    })
    return SimpleNamespace(fetch=fetch, source=source, staged=staged, served=served,
                           state=state, config=config)


def _served_comicinfo(data, tmp_path):
    path = tmp_path / "served.cbz"
    path.write_bytes(data)
    with zipfile.ZipFile(path) as archive:
        return etree.fromstring(archive.read("ComicInfo.xml"))


def test_download_serves_merged_comicinfo_and_cleans_up(download_env, tmp_path):
    status, data = download_env.fetch()
    assert status == 200
    root = _served_comicinfo(data, tmp_path)
    assert _text(root, "Title") == "Library Title"
    assert _text(root, "Penciller") == "File Penciller"
    assert download_env.served[0].parent == download_env.staged
    assert list(download_env.staged.iterdir()) == []
    with zipfile.ZipFile(download_env.source) as original:
        assert original.read("ComicInfo.xml") == _EXISTING


def test_download_serves_original_when_comicinfo_matches(download_env):
    download_env.state.book = _empty_book(title="Old Title")
    status, data = download_env.fetch()
    assert status == 200
    assert data == download_env.source.read_bytes()
    assert download_env.served == [download_env.source]


def test_download_serves_original_when_embed_fails(download_env, monkeypatch):
    def boom(*_args):
        raise OSError("disk full")
    monkeypatch.setattr(comicinfo_embed, "write_cbz_with_comicinfo", boom)
    status, data = download_env.fetch()
    assert status == 200
    assert data == download_env.source.read_bytes()
    assert list(download_env.staged.iterdir()) == []


def test_download_serves_original_when_embedding_is_disabled(download_env):
    download_env.config.config_embed_metadata = False
    status, data = download_env.fetch()
    assert status == 200
    assert data == download_env.source.read_bytes()
