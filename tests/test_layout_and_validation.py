import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageFont
import pd01_mac as app
from printer_faults import PrinterFault
from print_layout import MAX_RASTER_HEIGHT, image_raster


class LayoutValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        font = patch.object(app.driver, '_load_font', side_effect=lambda size: ImageFont.load_default(size=size))
        font.start()
        self.addCleanup(font.stop)

    def test_text_errors_have_specific_codes(self):
        for text, size, align, code in [('', 36, 'center', 'text_empty'),
                ('x' * 2001, 36, 'center', 'text_too_long'), ('hello', '36', 'center', 'font_size_invalid'),
                ('hello', 36, 'other', 'alignment_invalid')]:
            with self.subTest(code=code), self.assertRaises(PrinterFault) as caught:
                app.driver.render_text(text, size, align)
            self.assertEqual(caught.exception.code, code)
            self.assertFalse(caught.exception.as_result()['paper_used'])

    def test_text_many_newlines_is_rejected_before_a_long_raster_is_allocated(self):
        with self.assertRaises(PrinterFault) as caught:
            app.driver.render_text('hello' + '\n' * 500, 36, 'center')
        self.assertEqual(caught.exception.code, 'paper_length_exceeded')

    def test_missing_and_corrupt_images_are_distinguished(self):
        with self.assertRaises(PrinterFault) as caught:
            app.prepare_image(self.root, self.root / 'missing.png')
        self.assertEqual(caught.exception.code, 'image_not_found')
        bad = self.root / 'bad.png'
        bad.write_bytes(b'not a picture')
        with self.assertRaises(PrinterFault) as caught:
            app.prepare_image(self.root, bad)
        self.assertEqual(caught.exception.code, 'image_invalid')

    def test_too_long_image_is_not_silently_truncated(self):
        path = self.root / 'long.png'
        Image.new('1', (384, MAX_RASTER_HEIGHT + 1), 255).save(path)
        with self.assertRaises(PrinterFault) as caught:
            image_raster(path)
        self.assertEqual(caught.exception.code, 'paper_length_exceeded')

    def test_mixed_preview_preserves_block_order_and_image_alignment(self):
        image_path = self.root / 'square.png'
        Image.new('1', (50, 50), 0).save(image_path)
        manifest = self.root / 'layout.json'
        manifest.write_text(json.dumps({'blocks': [
            {'type': 'text', 'text': 'Title', 'font_size': 36},
            {'type': 'image', 'path': 'square.png', 'width': 100},
            {'type': 'spacer', 'height': 20},
            {'type': 'text', 'text': 'Caption', 'font_size': 24}]}))
        first_height = app.driver.render_text('Title', 36, 'center')[2]
        caption_height = app.driver.render_text('Caption', 24, 'center')[2]
        with patch.object(app.driver, 'print_png') as sender:
            job = app.prepare_mixed(self.root / 'state', manifest)
            sender.assert_not_called()
        self.assertEqual(job['kind'], 'mixed')
        self.assertIsNone(job['font_size'])
        self.assertEqual(job['text'], 'Title\nCaption')
        self.assertEqual(job['height'], first_height + 100 + 20 + caption_height)
        with Image.open(job['preview']) as preview:
            self.assertEqual(preview.size, (384, job['height']))
            self.assertEqual(preview.getpixel((192, first_height + 50)), 0)
            self.assertEqual(preview.getpixel((0, first_height + 50)), 255)
            self.assertEqual(preview.getpixel((192, first_height + 110)), 255)

    def test_invalid_layout_and_spacer_only_layout_do_not_create_jobs(self):
        for blocks in [[{'type': 'unknown'}], [{'type': 'spacer'}],
                       [{'type': 'image', 'path': 'x.png', 'width': True}],
                       [{'type': 'spacer', 'height': -1}]]:
            manifest = self.root / 'bad-layout.json'
            manifest.write_text(json.dumps({'blocks': blocks}))
            with self.subTest(blocks=blocks), self.assertRaises(PrinterFault):
                app.prepare_mixed(self.root / 'state', manifest)
        self.assertFalse((self.root / 'state' / 'jobs').exists())

    def test_combined_layout_length_limit_is_checked(self):
        Image.new('1', (384, 2100), 255).save(self.root / 'tall.png')
        manifest = self.root / 'too-long.json'
        manifest.write_text(json.dumps([{'type': 'image', 'path': 'tall.png'}] * 2))
        with self.assertRaises(PrinterFault) as caught:
            app.prepare_mixed(self.root / 'state', manifest)
        self.assertEqual(caught.exception.code, 'paper_length_exceeded')


if __name__ == '__main__':
    unittest.main()
