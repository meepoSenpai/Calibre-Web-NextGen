# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Embed the library's current metadata into a CBZ's ComicInfo.xml on download.

``calibredb export`` writes comic metadata as ComicBookInfo JSON in the zip
comment, which most comic readers (Komga, Kavita, Panels, KOReader, Mihon)
ignore; they read ComicInfo.xml. This module merges the library's metadata
into the archive's existing ComicInfo.xml and serves a staged copy.

Merge policy: a field the library has filled replaces the XML's value; a field
the library leaves empty keeps whatever the XML already carries. Every element
the library does not model (Penciller, Inker, ``<Pages>``, Manga, AgeRating,
unknown extensions, ...) is preserved untouched.

When the merged values already match the archive's XML, no copy is made and
the original file is served as is, so a large omnibus is only rewritten when
there is something to change.
"""

import os
import shutil
import zipfile
from uuid import uuid4

from lxml import etree

from .. import logger, isoLanguages
from ..file_helper import get_temp_dir

log = logger.create()

COMICINFO_NAME = "ComicInfo.xml"

# ComicInfo v2.1 xs:sequence order. New elements are inserted at their schema
# position so strict validators keep accepting the file.
_SCHEMA_ORDER = (
    "Title", "Series", "Number", "Count", "Volume", "AlternateSeries",
    "AlternateNumber", "AlternateCount", "Summary", "Notes", "Year", "Month",
    "Day", "Writer", "Penciller", "Inker", "Colorist", "Letterer",
    "CoverArtist", "Editor", "Translator", "Publisher", "Imprint", "Genre",
    "Tags", "Web", "PageCount", "LanguageISO", "Format", "BlackAndWhite",
    "Manga", "Characters", "Teams", "Locations", "ScanInformation",
    "StoryArc", "StoryArcNumber", "SeriesGroup", "AgeRating", "Pages",
    "CommunityRating", "MainCharacterOrTeam", "Review", "GTIN",
)
_SCHEMA_INDEX = {name: index for index, name in enumerate(_SCHEMA_ORDER)}

# Comma-separated list fields compare as lists, so "A,B" matches "A, B".
_LIST_FIELDS = frozenset(("Writer", "Tags"))
_NUMBER_FIELDS = frozenset(("Number", "Year", "Month", "Day", "CommunityRating"))

# Calibre's placeholder author for books without one; not real metadata.
_PLACEHOLDER_AUTHORS = frozenset(("unknown",))

_EMPTY_COMICINFO = (
    b'<?xml version="1.0" encoding="utf-8"?>\n'
    b'<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
    b'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>'
)


def _format_number(value):
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def library_fields(book):
    """Map a Calibre book to ComicInfo element values, omitting empty fields."""
    from ..helper import html_to_text

    fields = {}
    if (book.title or "").strip():
        fields["Title"] = book.title.strip()
    if book.series:
        fields["Series"] = book.series[0].name
        if book.series_index is not None:
            fields["Number"] = _format_number(book.series_index)
    if book.comments and (book.comments[0].text or "").strip():
        summary = html_to_text(book.comments[0].text).strip()
        if summary:
            fields["Summary"] = summary
    pubdate = getattr(book, "pubdate", None)
    if pubdate is not None and pubdate.year > 101:
        fields["Year"] = str(pubdate.year)
        fields["Month"] = str(pubdate.month)
        fields["Day"] = str(pubdate.day)
    authors = [author.name for author in book.authors
               if author is not None and (author.name or "").strip()
               and author.name.strip().lower() not in _PLACEHOLDER_AUTHORS]
    if authors:
        fields["Writer"] = ", ".join(authors)
    if book.publishers:
        fields["Publisher"] = book.publishers[0].name
    if book.tags:
        fields["Tags"] = ", ".join(tag.name for tag in book.tags)
    if book.languages:
        try:
            part1 = isoLanguages.get(part3=book.languages[0].lang_code).part1
        except (KeyError, AttributeError, LookupError):
            part1 = None
        if part1:
            fields["LanguageISO"] = part1
    if book.ratings and book.ratings[0].rating:
        fields["CommunityRating"] = _format_number(book.ratings[0].rating / 2)
    isbn = next((identifier.val for identifier in book.identifiers
                 if identifier.type.lower() == "isbn" and identifier.val), None)
    if isbn:
        fields["GTIN"] = isbn
    return fields


def _normalized(name, value):
    value = (value or "").strip()
    if name in _LIST_FIELDS:
        return [item.strip().casefold() for item in value.split(",") if item.strip()]
    if name in _NUMBER_FIELDS:
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _parse(xml_bytes):
    parser = etree.XMLParser(resolve_entities=False, no_network=True,
                             remove_blank_text=False)
    return etree.fromstring(xml_bytes, parser)


def _child(root, name):
    return next((element for element in root
                 if isinstance(element.tag, str)
                 and etree.QName(element).localname == name), None)


def _insert_in_schema_order(root, element):
    position = _SCHEMA_INDEX[element.tag]
    for index, sibling in enumerate(root):
        if not isinstance(sibling.tag, str):
            continue
        sibling_position = _SCHEMA_INDEX.get(etree.QName(sibling).localname)
        if sibling_position is not None and sibling_position > position:
            root.insert(index, element)
            return
    root.append(element)


def merge_comicinfo(existing_xml, fields):
    """Merge library ``fields`` into ``existing_xml`` (bytes or None).

    Returns the merged ComicInfo.xml bytes, or None when the existing XML
    already carries every library value and the archive can be served as is.
    """
    root = None
    if existing_xml:
        try:
            root = _parse(existing_xml)
        except etree.XMLSyntaxError as ex:
            log.warning("Ignoring unparseable ComicInfo.xml: %s", ex)
        if root is not None and etree.QName(root).localname != "ComicInfo":
            log.warning("Ignoring ComicInfo.xml with unexpected root <%s>", root.tag)
            root = None
    if root is None:
        if not fields:
            return None
        root = _parse(_EMPTY_COMICINFO)

    changed = False
    for name in _SCHEMA_ORDER:
        if name not in fields:
            continue
        element = _child(root, name)
        if element is not None and _normalized(name, element.text) == _normalized(name, fields[name]):
            continue
        changed = True
        if element is None:
            element = etree.Element(name)
            _insert_in_schema_order(root, element)
        element.text = fields[name]
    if not changed:
        return None
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def _is_comicinfo(name):
    return os.path.basename(name).lower() == COMICINFO_NAME.lower()


def _find_comicinfo(archive):
    names = archive.namelist()
    if COMICINFO_NAME in names:
        return COMICINFO_NAME
    return next((name for name in names if _is_comicinfo(name)), None)


def write_cbz_with_comicinfo(source_path, target_path, comicinfo_xml):
    """Copy ``source_path`` to ``target_path`` with ``comicinfo_xml`` at the root.

    Entries are streamed one at a time so multi-gigabyte archives are never held
    in memory. Any existing ComicInfo.xml (root or nested) is replaced by the
    single root-level copy. The zip comment, which may carry ComicBookInfo, is
    kept.
    """
    with zipfile.ZipFile(source_path, "r") as source, \
            zipfile.ZipFile(target_path, "w", allowZip64=True) as target:
        target.comment = source.comment
        for info in source.infolist():
            if _is_comicinfo(info.filename):
                continue
            new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            new_info.compress_type = info.compress_type
            new_info.external_attr = info.external_attr
            new_info.comment = info.comment
            if info.is_dir():
                target.writestr(new_info, b"")
                continue
            # The declared size lets zipfile pick zip64 headers up front for
            # entries over 2 GiB.
            new_info.file_size = info.file_size
            with source.open(info) as src, target.open(new_info, "w") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
        info = zipfile.ZipInfo(COMICINFO_NAME)
        info.compress_type = zipfile.ZIP_DEFLATED
        target.writestr(info, comicinfo_xml)


def embed_cbz_metadata(book, source_path, extension="cbz"):
    """Stage a CBZ copy with the library's metadata merged into ComicInfo.xml.

    Returns ``(directory, basename)`` of the staged copy, the same shape as
    ``do_calibre_export``, or None when the original should be served as is
    (metadata already matches, or the file is not a readable zip).
    """
    if not zipfile.is_zipfile(source_path):
        log.warning("CBZ %s is not a zip archive; serving it unchanged", source_path)
        return None
    with zipfile.ZipFile(source_path, "r") as archive:
        comicinfo_name = _find_comicinfo(archive)
        existing_xml = archive.read(comicinfo_name) if comicinfo_name else None
    merged = merge_comicinfo(existing_xml, library_fields(book))
    if merged is None:
        log.debug("ComicInfo.xml of book %s already matches the library", book.id)
        return None

    tmp_dir = get_temp_dir()
    os.makedirs(tmp_dir, exist_ok=True)
    download_name = str(uuid4())
    target_path = os.path.join(tmp_dir, download_name + "." + extension)
    try:
        write_cbz_with_comicinfo(source_path, target_path, merged)
    except BaseException:
        try:
            os.remove(target_path)
        except OSError:
            pass
        raise
    return tmp_dir, download_name
