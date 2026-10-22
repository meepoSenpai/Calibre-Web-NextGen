### Added

- **Downloaded CBZ comics carry your edited metadata in ComicInfo.xml.** With
  "Embed metadata" on, web, OPDS and Kobo downloads merge the library's title,
  series and number, summary, publication date, authors, publisher, tags,
  language, rating and ISBN into the archive's ComicInfo.xml, which comic
  readers such as Komga, Kavita, Panels and KOReader read. A field filled in the
  library replaces the file's value; an empty one keeps what the file already
  has, and everything the library does not track (artists, page layout, manga
  reading direction) is kept. When the file already matches, it is served
  unchanged without copying. Google Drive libraries and other comic formats
  are served as before.
