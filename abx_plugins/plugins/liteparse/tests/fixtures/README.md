`ocr.avif` is an ArchiveBox-generated, 800×260 AVIF image containing:

    The quick brown fox jumps
    over the lazy dog.
    ArchiveBox OCR fixture 2026

Created with ImageMagick 7, black 36-point text on white, AVIF quality 95.
The checked-in bytes keep decoder tests independent of AVIF encoder support
on the test host. The tests verify the actual AVIF signature, extracted text,
and preservation of the archived source bytes, including a `.jpg` filename.

`ocr.png` is the existing Tesseract
[eurotext.png test image](https://raw.githubusercontent.com/tesseract-ocr/test/master/testing/eurotext.png)
with metadata stripped and its opaque alpha channel removed using
`magick eurotext.png -strip PNG24:ocr.png` (pixel comparison: zero differences).
This removes its 300-DPI metadata, which JPEG XL does not retain and which
otherwise changes LiteParse's spatial whitespace despite identical pixels.
`ocr.jxl` and `ocr-container.jxl` losslessly encode those same pixels with
`cjxl 0.11.2 -d 0 -e 1`, using `--container=0` and `--container=1`
respectively. Both encodings must retain the same meaningful OCR words and
original source bytes, even under a `.jpg` filename, and match the reference
PNG's complete OCR text byte for byte.

`animated.jxl` encodes a real two-frame GIF of eurotext.png and its 180-degree
rotation, with a 100-centisecond delay per frame (`cjxl -d 0 -e 1`). djxl
converts it into a two-frame APNG. LiteParse cannot extract every APNG frame,
so the hook must report that known incomplete conversion as a failure and
still retain OCR from other readable sources.
