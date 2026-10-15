`ocr.avif` is an ArchiveBox-generated, 800×260 AVIF image containing:

    The quick brown fox jumps
    over the lazy dog.
    ArchiveBox OCR fixture 2026

Created with ImageMagick 7, black 36-point text on white, AVIF quality 95.
The checked-in bytes keep decoder tests independent of AVIF encoder support
on the test host. The tests verify the actual AVIF signature, extracted text,
and preservation of the archived source bytes, including a `.jpg` filename.
