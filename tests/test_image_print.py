from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import pd01_mac as app


class ImagePreviewTests(unittest.TestCase):
    def test_image_preview_matches_printer_rows_and_keeps_aspect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.png'
            image = Image.new('RGB', (768, 384), 'white')
            image.paste('black', (100, 100, 300, 300))
            image.save(source)
            with patch.object(app.driver, 'print_png') as sender:
                job = app.prepare_image(root / 'state', source)
                sender.assert_not_called()
            self.assertEqual(job['kind'], 'image')
            self.assertIsNone(job['font_size'])
            self.assertEqual((job['width'], job['height']), (384, 192))
            with Image.open(job['preview']) as preview:
                self.assertEqual(preview.mode, '1')
                self.assertEqual(preview.getpixel((100, 100)), 0)
                self.assertEqual(preview.getpixel((0, 0)), 255)
            rows = app.driver.png_to_rows(Path(job['preview']).read_bytes())
            self.assertEqual(len(rows), 192)
            self.assertTrue(all(len(row) == 48 for row in rows))

    def test_transparency_is_printed_as_white(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.png'
            Image.new('RGBA', (384, 384), (0, 0, 0, 0)).save(source)
            job = app.prepare_image(root / 'state', source)
            with Image.open(job['preview']) as preview:
                self.assertEqual(preview.getextrema(), (255, 255))


if __name__ == '__main__':
    unittest.main()
