"""Generate the Windows icon and the .exe version resource for the build.

Usage: python prepare.py <app.py> <output dir>
Writes ramuzu.ico and version_info.txt into <output dir>.
"""
import os
import re
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QBuffer, QByteArray, QIODevice, QPointF, Qt
from PyQt5.QtGui import QColor, QGuiApplication, QImage, QPainter, QPolygonF

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def render_png(size):
    """Same artwork as make_app_icon() in the app, drawn at the requested size."""
    s = size / 64
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#2F6FEB"))
    painter.drawRoundedRect(int(4 * s), int(4 * s), round(56 * s), round(56 * s), 14 * s, 14 * s)
    painter.setBrush(QColor("#FFFFFF"))
    painter.drawPolygon(QPolygonF([QPointF(26 * s, 20 * s), QPointF(26 * s, 44 * s), QPointF(46 * s, 32 * s)]))
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def write_ico(path):
    images = [render_png(size) for size in ICON_SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in zip(ICON_SIZES, images):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    path.write_bytes(header + entries + blobs)


def write_version_info(path, name, version, author):
    parts = [int(p) for p in re.findall(r"\d+", version)][:4]
    parts += [0] * (4 - len(parts))
    numbers = ", ".join(map(str, parts))
    dotted = ".".join(map(str, parts))
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({numbers}), prodvers=({numbers}), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', '{author}'),
      StringStruct('FileDescription', '{name}'),
      StringStruct('FileVersion', '{dotted}'),
      StringStruct('InternalName', '{name}'),
      StringStruct('LegalCopyright', '{author}. GNU GPL v2'),
      StringStruct('OriginalFilename', '{name}.exe'),
      StringStruct('ProductName', '{name}'),
      StringStruct('ProductVersion', '{dotted}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")


def read_constant(source, name):
    match = re.search(rf'^{name}\s*=\s*"([^"]+)"', source, re.M)
    if not match:
        sys.exit(f"{name} not found in the app source")
    return match.group(1)


def main():
    app_py, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    source = app_py.read_text(encoding="utf-8")
    name = read_constant(source, "APP_NAME")
    version = read_constant(source, "APP_VERSION")
    author = read_constant(source, "APP_AUTHOR")

    app = QGuiApplication(sys.argv[:1])  # needed for painting
    write_ico(out_dir / "ramuzu.ico")
    write_version_info(out_dir / "version_info.txt", name, version, author)
    del app
    print(version)


if __name__ == "__main__":
    main()
