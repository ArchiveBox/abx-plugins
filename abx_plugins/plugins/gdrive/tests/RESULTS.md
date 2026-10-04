# Google Drive acceptance results

Verified 2026-10-04 with real Chrome and provider downloads on macOS.

- Public nested gdown folder: six raw files plus empty and nested directories.
  Filenames, sizes, hashes, image signatures and the text file were checked.
  Neither the output directory nor the owned browser download retains its
  transport ZIP. The settled browser tab is reused.
- A private test folder downloaded successfully using Google cookies from an
  existing Brave login, without API credentials or OAuth setup. Its two files
  matched the uploaded text/CSV byte for byte; an anonymous session was denied.
  This authentication check preceded the raw-storage change.
- Original ArchiveBox capture URLs were rerun with the full configured plugin
  set after clearing old capture-level plugin restrictions. The ordinary file
  browser and rg can read the extracted text, and the live Sonic service returns
  the original Drive capture for `mollis` from its saved text file.
- Real LiteParse/Tesseract tests cover images under all three export roots and
  embedded export images. A real ZIP-to-OCR-to-Sonic integration test verifies
  TXT, Markdown, HTML and OCR terms without retaining the transport ZIP.
- ZIPs saved as actual content use ArchiveBox's generic static-file ZIP browser,
  which has a real Chromium ZIP64/range-read test in the ArchiveBox repository.

The full Drive capture exposed an unrelated yt-dlp GoogleDrive extractor failure
(`expected string or bytes-like object, got 'bool'`). Google Drive extraction,
OCR, indexing, and the other configured outputs completed. Provider preparation
limits and non-English controls remain unverified; no recursive crawl is used.
