"""Create a tiny synthetic mixed-input Q01 in a new directory; no Fusion calls."""
import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new Q01 public directory; existing output is never overwritten")
    args = parser.parse_args(argv)
    root = args.output.absolute()
    root.mkdir(parents=True, exist_ok=False)
    (root / "views").mkdir()
    (root / "TASK.md").write_text(
        "# Synthetic input example\n\nCreate one solid rectangular block: length 40 mm, width 20 mm, height 10 mm.\n"
        "Read front and side images in the declared order, then the DXF outline.\n"
        "Use annotated dimensions, never image pixel lengths. The DXF outline is in mm.\n"
        "This example tests input transport; it contains no reference model or scoring data.\n", encoding="utf-8")
    for name, title, width_label, height_label in (
            ("front", "FRONT", "40 mm", "10 mm"), ("side", "SIDE", "20 mm", "10 mm")):
        image = Image.new("RGB", (400, 240), "white")
        draw = ImageDraw.Draw(image)
        draw.text((160, 20), title, fill="black")
        draw.rectangle((65, 75, 335, 165), outline="black", width=3)
        draw.text((175, 190), width_label, fill="black")
        draw.text((340, 110), height_label, fill="black")
        image.save(root / f"views/{name}.png")
    (root / "outline.dxf").write_text(
        "0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n4\n0\nENDSEC\n"
        "0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n100\nAcDbEntity\n8\n0\n"
        "100\nAcDbPolyline\n90\n4\n70\n1\n10\n0\n20\n0\n10\n40\n20\n0\n"
        "10\n40\n20\n20\n10\n0\n20\n20\n0\nENDSEC\n0\nEOF\n", encoding="ascii")

    def descriptor(identifier, role, relative, mime, label):
        raw = (root / relative).read_bytes()
        return {"id": identifier, "role": role, "path": relative, "mime": mime,
                "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "display_name": label}

    manifest = {"schema": "fusion-public-inputs/1", "case_id": "Q01",
                "task": descriptor("task", "task", "TASK.md", "text/markdown", "Task"),
                "attachments": [descriptor("front", "view", "views/front.png", "image/png", "Front view"),
                                descriptor("side", "view", "views/side.png", "image/png", "Side view"),
                                descriptor("outline", "drawing", "outline.dxf", "application/dxf", "DXF outline")]}
    (root / "inputs.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(root / "inputs.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
