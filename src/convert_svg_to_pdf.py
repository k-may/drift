from pathlib import Path
import cairosvg

input_dir = Path("./output/freestyle_scaled_svg")
output_dir = Path("./output/scaled_pdf")

print(f"Converting SVG files from '{input_dir}' to PDF files in '{output_dir}'...")
output_dir.mkdir(exist_ok=True)

for svg_file in input_dir.glob("*.svg"):
    pdf_file = output_dir / f"{svg_file.stem}.pdf"

    cairosvg.svg2pdf(
        url=str(svg_file),
        write_to=str(pdf_file)
    )

    print(f"Converted: {svg_file.name} -> {pdf_file.name}")