# PapersDL PDF fixtures

`correction.pdf` is a generated one-page PDF. Its Subject metadata and header
declare the correction DOI; its body cites the original DOI. Generate it with
PyMuPDF (the fixture was generated with 1.28.2):

```python
import pymupdf

original = "10.5555/abx-papersdl-original"
correction = original + "e"
with pymupdf.open() as document:
    document.set_metadata({"title": "Correction", "subject": f"DOI: {correction}"})
    page = document.new_page()
    page.insert_text((72, 72), f"Correction\nDOI: {correction}")
    page.insert_text((72, 180), f"This correction cites the original article.\nDOI: {original}")
    document.save("correction.pdf", deflate=True, no_new_id=True)
```
