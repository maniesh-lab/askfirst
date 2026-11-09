"""Write the fake persona's sample files with no extra dependencies.

- `profiles/files/resume.pdf`: a tiny valid one-page PDF, written in raw PDF syntax
- `profiles/files/product.png`: a small solid-colour PNG, written with zlib

Run: `python -m scripts.make_sample_files`. The output is committed, so this only needs to run
again if the files change.
"""

import struct
import zlib
from pathlib import Path

FILES = Path(__file__).resolve().parents[1] / "profiles" / "files"


def make_pdf(lines: list[str]) -> bytes:
    text = "\n".join(
        f"BT /F1 12 Tf 72 {720 - 18 * i} Td ({line.replace('(', '[').replace(')', ']')}) Tj ET"
        for i, line in enumerate(lines)
    ).encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def make_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes(rgb) * width
    raw = zlib.compress(row * height, 9)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def main() -> None:
    FILES.mkdir(parents=True, exist_ok=True)
    (FILES / "resume.pdf").write_bytes(
        make_pdf(
            [
                "Riya Testwell (fictional test persona)",
                "Support Specialist, Example Labs, 2023 - present",
                "Office Assistant, Sample Traders, 2021 - 2023",
                "Bachelor of Business Studies, Example College, 2021",
            ]
        )
    )
    (FILES / "product.png").write_bytes(make_png(64, 48, (36, 87, 197)))
    print(f"wrote sample files to {FILES}")


if __name__ == "__main__":
    main()
