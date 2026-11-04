"""Download real, reusable sample reference photographs for the ScrapFit demo.

Important: This script downloads the photographs when run on your computer; the photographs are not bundled inside this ZIP.

Run from the ScrapFit project root:
    python download_sample_photos.py

Photos are downloaded from Wikimedia Commons thumbnail endpoints. The accompanying
IMAGE_CREDITS.md records source pages, creators, and licenses. These are illustrative
reference photos, not photos of the exact fictional inventory items.
"""
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
IMAGE_DIR = ROOT / "scrapfit_images"

PHOTOS = {
    "wood_offcuts_01.jpg": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/71/A_pile_of_unprocessed_wood_in_a_carpentry_workshop.jpg/960px-A_pile_of_unprocessed_wood_in_a_carpentry_workshop.jpg",
    "wood_offcuts_02.jpg": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e2/Workshop_filled_with_wood_scraps_and_tools_in_a_rustic_environment.jpg/960px-Workshop_filled_with_wood_scraps_and_tools_in_a_rustic_environment.jpg",
    "wood_offcuts_03.jpg": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/d3/Messy_pile_of_wooden_parts_in_a_carpentry_workshop.jpg/960px-Messy_pile_of_wooden_parts_in_a_carpentry_workshop.jpg",
    "wood_offcuts_04.jpg": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e3/Crafting_wooden_pieces_in_a_workshop.jpg/960px-Crafting_wooden_pieces_in_a_workshop.jpg",
    "wood_offcuts_05.jpg": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/69/Wood_workshop_view.jpg/960px-Wood_workshop_view.jpg",
    "metal_offcuts_01.jpg": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1f/Sheet_metal_used.jpg/960px-Sheet_metal_used.jpg",
    "metal_offcuts_02.jpg": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a4/Collection_of_leftover_scrap_metal_items.jpg/960px-Collection_of_leftover_scrap_metal_items.jpg",
    "metal_offcuts_03.jpg": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e1/Scrap_Metal.JPG/960px-Scrap_Metal.JPG",
    "metal_offcuts_04.jpg": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/86/Rusty_scraps_of_iron.jpg/960px-Rusty_scraps_of_iron.jpg",
    "metal_offcuts_05.jpg": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/2d/Metal_parts_are_stacked_in_a_workshop_closeup.jpg/960px-Metal_parts_are_stacked_in_a_workshop_closeup.jpg",
}


def main():
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    failures = []
    for filename, url in PHOTOS.items():
        destination = IMAGE_DIR / filename
        request = Request(url, headers={"User-Agent": "ScrapFitEducationalDemo/1.0 (sample photo downloader)"})
        try:
            with urlopen(request, timeout=45) as response:
                content_type = response.headers.get("Content-Type", "")
                data = response.read()
            if not content_type.lower().startswith("image/") or len(data) < 5_000:
                raise ValueError(f"Unexpected response ({content_type}, {len(data)} bytes)")
            destination.write_bytes(data)
            print(f"Downloaded {filename} ({len(data) // 1024} KB)")
        except Exception as exc:
            failures.append((filename, str(exc)))
            print(f"FAILED {filename}: {exc}")

    print(f"\nPhoto folder: {IMAGE_DIR}")
    print(f"Downloaded {len(PHOTOS) - len(failures)} of {len(PHOTOS)} photographs.")
    if failures:
        print("If a download fails, open the matching Wikimedia Commons source page listed in IMAGE_CREDITS.md, click the preview/original file, save it into scrapfit_images, and use the exact filename shown above.")
        raise SystemExit(1)
    print("Done. These are real photographs used as illustrative demo references; they do not depict exact inventory items.")


if __name__ == "__main__":
    main()
