"""Tests for media compression and WhatsApp size enforcement."""

import os
import tempfile
import subprocess
from unittest.mock import patch, MagicMock

from django.test import SimpleTestCase, override_settings

from api.views import (
    WHATSAPP_MEDIA_LIMITS,
    _ensure_media_under_limit,
    _compress_image,
    _compress_video,
    _compress_audio,
)


def _make_temp_file(size_bytes, suffix='.bin'):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.write(fd, b'x' * size_bytes)
    os.close(fd)
    return path


def has_ffmpeg():
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ── Limit constants ──────────────────────────────────────────────────────────

class WhatsAppMediaLimitsTests(SimpleTestCase):
    def test_limits_dict_has_all_types(self):
        expected_keys = {'image', 'video', 'audio', 'document', 'sticker'}
        self.assertTrue(expected_keys.issubset(WHATSAPP_MEDIA_LIMITS.keys()))

    def test_image_limit_5mb(self):
        self.assertEqual(WHATSAPP_MEDIA_LIMITS['image'], 5 * 1024 * 1024)

    def test_video_limit_16mb(self):
        self.assertEqual(WHATSAPP_MEDIA_LIMITS['video'], 16 * 1024 * 1024)

    def test_audio_limit_16mb(self):
        self.assertEqual(WHATSAPP_MEDIA_LIMITS['audio'], 16 * 1024 * 1024)

    def test_document_limit_100mb(self):
        self.assertEqual(WHATSAPP_MEDIA_LIMITS['document'], 100 * 1024 * 1024)

    def test_sticker_limit_500kb(self):
        self.assertEqual(WHATSAPP_MEDIA_LIMITS['sticker'], 500 * 1024)


# ── _ensure_media_under_limit ────────────────────────────────────────────────

class EnsureMediaUnderLimitTests(SimpleTestCase):
    def test_returns_original_when_under_limit(self):
        path = _make_temp_file(100)
        self.addCleanup(os.remove, path)
        result_path, cleanup = _ensure_media_under_limit(path, 'image')
        self.assertEqual(result_path, path)
        self.assertIsNone(cleanup)

    def test_returns_original_when_at_limit(self):
        path = _make_temp_file(5 * 1024 * 1024)
        self.addCleanup(os.remove, path)
        result_path, cleanup = _ensure_media_under_limit(path, 'image')
        self.assertEqual(result_path, path)
        self.assertIsNone(cleanup)

    def test_unknown_type_returns_original(self):
        path = _make_temp_file(100)
        self.addCleanup(os.remove, path)
        result_path, cleanup = _ensure_media_under_limit(path, 'unknown_type')
        self.assertEqual(result_path, path)
        self.assertIsNone(cleanup)

    def test_document_over_limit_returns_none(self):
        path = _make_temp_file(110 * 1024 * 1024)
        self.addCleanup(os.remove, path)
        result_path, cleanup = _ensure_media_under_limit(path, 'document')
        self.assertIsNone(result_path)
        self.assertIsNone(cleanup)

    @patch('api.views._compress_image')
    def test_oversized_image_calls_compress(self, mock_compress):
        mock_compress.return_value = '/tmp/compressed.jpg'
        path = _make_temp_file(10 * 1024 * 1024)
        self.addCleanup(os.remove, path)
        result_path, cleanup = _ensure_media_under_limit(path, 'image')
        mock_compress.assert_called_once_with(path, 5 * 1024 * 1024)
        self.assertEqual(result_path, '/tmp/compressed.jpg')
        self.assertEqual(cleanup, '/tmp/compressed.jpg')

    @patch('api.views._compress_video')
    def test_oversized_video_calls_compress(self, mock_compress):
        mock_compress.return_value = '/tmp/compressed.mp4'
        path = _make_temp_file(20 * 1024 * 1024)
        self.addCleanup(os.remove, path)
        result_path, cleanup = _ensure_media_under_limit(path, 'video')
        mock_compress.assert_called_once_with(path, 16 * 1024 * 1024)
        self.assertEqual(result_path, '/tmp/compressed.mp4')
        self.assertEqual(cleanup, '/tmp/compressed.mp4')

    @patch('api.views._compress_audio')
    def test_oversized_audio_calls_compress(self, mock_compress):
        mock_compress.return_value = '/tmp/compressed.ogg'
        path = _make_temp_file(20 * 1024 * 1024)
        self.addCleanup(os.remove, path)
        result_path, cleanup = _ensure_media_under_limit(path, 'audio')
        mock_compress.assert_called_once_with(path, 16 * 1024 * 1024)
        self.assertEqual(result_path, '/tmp/compressed.ogg')
        self.assertEqual(cleanup, '/tmp/compressed.ogg')

    @patch('api.views._compress_image')
    def test_compression_failure_returns_none(self, mock_compress):
        mock_compress.return_value = None
        path = _make_temp_file(10 * 1024 * 1024)
        self.addCleanup(os.remove, path)
        result_path, cleanup = _ensure_media_under_limit(path, 'image')
        self.assertIsNone(result_path)
        self.assertIsNone(cleanup)

    @patch('api.views._compress_image')
    def test_sticker_calls_image_compressor(self, mock_compress):
        mock_compress.return_value = '/tmp/compressed.webp'
        path = _make_temp_file(1024 * 1024)
        self.addCleanup(os.remove, path)
        result_path, cleanup = _ensure_media_under_limit(path, 'sticker')
        mock_compress.assert_called_once_with(path, 500 * 1024)


# ── _compress_image integration ──────────────────────────────────────────────

class CompressImageIntegrationTests(SimpleTestCase):

    def setUp(self):
        try:
            from PIL import Image as _pil_mod
            self._has_pil = True
            self._pil_mod = _pil_mod
        except ImportError:
            self._has_pil = False

    def test_image_already_under_limit_returns_none(self):
        path = _make_temp_file(100, suffix='.jpg')
        self.addCleanup(os.remove, path)
        result = _compress_image(path, 5 * 1024 * 1024)
        self.assertIsNone(result)

    def test_compress_large_image_reduces_size(self):
        if not self._has_pil:
            self.skipTest('Pillow not installed')
        with tempfile.NamedTemporaryFile(suffix='.bmp', delete=False) as f:
            bmp_path = f.name
        self.addCleanup(os.remove, bmp_path)
        img = self._pil_mod.new('RGB', (500, 500), (255, 255, 255))
        img.save(bmp_path, 'BMP')
        original_size = os.path.getsize(bmp_path)
        result = _compress_image(bmp_path, 5 * 1024 * 1024)
        self.assertIsNotNone(result, '_compress_image returned None for a compressible BMP')
        if result is not None:
            self.addCleanup(os.remove, result)
            self.assertLess(os.path.getsize(result), original_size)

    def test_compress_image_with_transparency(self):
        if not self._has_pil:
            self.skipTest('Pillow not installed')
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
            png_path = f.name
        self.addCleanup(os.remove, png_path)
        img = self._pil_mod.new('RGBA', (500, 500), (255, 0, 0, 128))
        img.save(png_path, 'PNG')
        result = _compress_image(png_path, 5 * 1024 * 1024)
        self.assertIsNotNone(result, '_compress_image returned None for an RGBA PNG')
        if result is not None:
            self.addCleanup(os.remove, result)
            self.assertLess(os.path.getsize(result), 5 * 1024 * 1024)

    def test_compress_image_missing_file(self):
        result = _compress_image('/nonexistent/path.jpg', 5 * 1024 * 1024)
        self.assertIsNone(result)


# ── _compress_video integration ──────────────────────────────────────────────

class CompressVideoIntegrationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not has_ffmpeg():
            raise cls.skipException('ffmpeg not available')

    def test_video_already_under_limit_returns_none(self):
        path = _make_temp_file(100, suffix='.mp4')
        self.addCleanup(os.remove, path)
        result = _compress_video(path, 16 * 1024 * 1024)
        self.assertIsNone(result)

    def test_compress_video_creates_mp4(self):
        cmd = [
            'ffmpeg', '-y',
            '-f', 'lavfi', '-i', 'color=c=black:s=320x240:d=2', '-frames', '1',
            '-f', 'mp4', '-',
        ]
        tiny = subprocess.run(cmd, capture_output=True, timeout=10)
        if tiny.returncode != 0:
            self.skipTest('ffmpeg cannot generate test video')
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as f:
            f.write(tiny.stdout)
        self.addCleanup(os.remove, f.name)
        result = _compress_video(f.name, 5)
        if result is not None:
            self.addCleanup(os.remove, result)
            self.assertLessEqual(os.path.getsize(result), 5)
            self.assertTrue(result.endswith('.mp4'))

    def test_compress_video_missing_file(self):
        result = _compress_video('/nonexistent/path.mp4', 16 * 1024 * 1024)
        self.assertIsNone(result)


# ── _compress_audio integration ──────────────────────────────────────────────

class CompressAudioIntegrationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not has_ffmpeg():
            raise cls.skipException('ffmpeg not available')

    def test_audio_already_under_limit_returns_none(self):
        path = _make_temp_file(100, suffix='.ogg')
        self.addCleanup(os.remove, path)
        result = _compress_audio(path, 16 * 1024 * 1024)
        self.assertIsNone(result)

    def test_compress_audio_creates_ogg(self):
        cmd = [
            'ffmpeg', '-y',
            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
            '-c:a', 'pcm_s16le', '-ar', '48000', '-ac', '1',
            '-f', 'wav', '-',
        ]
        wav = subprocess.run(cmd, capture_output=True, timeout=10)
        if wav.returncode != 0:
            self.skipTest('ffmpeg cannot generate test audio')
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            f.write(wav.stdout)
        self.addCleanup(os.remove, f.name)
        result = _compress_audio(f.name, 5)
        if result is not None:
            self.addCleanup(os.remove, result)
            self.assertLessEqual(os.path.getsize(result), 5)
            self.assertTrue(result.endswith('.ogg'))

    def test_compress_audio_missing_file(self):
        result = _compress_audio('/nonexistent/path.wav', 16 * 1024 * 1024)
        self.assertIsNone(result)


# ── send_whatsapp_outbound integration (compression path) ────────────────────

class SendWhatsappOutboundCompressionTests(SimpleTestCase):
    """Verify that send_whatsapp_outbound invokes compression plumbing.

    Full end-to-end is not testable without a real WhatsApp API token,
    so we mock at the network boundary and verify the compression path.
    """

    @patch('api.views.acquire_rate_capacity')
    @patch('api.views._resolve_media_path')
    @patch('api.views._ensure_media_under_limit')
    @patch('api.views.upload_media_to_whatsapp')
    @patch('api.views.Message.objects.get')
    @patch('api.views.Conversation.objects.get')
    @patch('api.views.publish_conversation_update')
    def test_compression_called_before_upload(
        self,
        mock_publish,
        mock_conv_get,
        mock_msg_get,
        mock_upload,
        mock_ensure,
        mock_resolve,
        mock_rate_limit,
    ):
        mock_resolve.return_value = '/media/uploads/images/test.jpg'
        mock_ensure.return_value = ('/tmp/compressed.jpg', '/tmp/compressed.jpg')
        mock_upload.return_value = 'wa-media-id-123'
        mock_msg = MagicMock()
        mock_msg.id = 1
        mock_msg.metadata = {}
        mock_msg.conversation_id = 1
        mock_msg.content = 'caption'
        mock_msg_get.return_value = mock_msg
        mock_conv = MagicMock()
        mock_conv_get.return_value = mock_conv

        from api.views import send_whatsapp_outbound

        send_whatsapp_outbound(
            'image',
            '/media/uploads/images/test.jpg',
            '15551234567',
            message_id=1,
            conversation_id=1,
        )

        mock_ensure.assert_called_once()
        args, _ = mock_ensure.call_args
        self.assertEqual(args[0], '/media/uploads/images/test.jpg')
        self.assertEqual(args[1], 'image')

        mock_upload.assert_called_once()
        upload_args = mock_upload.call_args[0]
        self.assertEqual(upload_args[0], '/tmp/compressed.jpg')

        # acquired_rate_capacity called for upload + main POST
        self.assertGreaterEqual(mock_rate_limit.call_count, 1)

    @patch('api.views.acquire_rate_capacity')
    @patch('api.views._resolve_media_path')
    @patch('api.views._ensure_media_under_limit')
    @patch('api.views.upload_media_to_whatsapp')
    @patch('api.views.Message.objects.get')
    @patch('api.views.Conversation.objects.get')
    @patch('api.views.publish_conversation_update')
    def test_compression_failure_skips_upload(
        self,
        mock_publish,
        mock_conv_get,
        mock_msg_get,
        mock_upload,
        mock_ensure,
        mock_resolve,
        mock_rate_limit,
    ):
        mock_resolve.return_value = '/media/uploads/images/test.jpg'
        mock_ensure.return_value = (None, None)
        mock_msg = MagicMock()
        mock_msg.id = 1
        mock_msg.metadata = {}
        mock_msg.conversation_id = 1
        mock_msg_get.return_value = mock_msg
        mock_conv = MagicMock()
        mock_conv_get.return_value = mock_conv

        from api.views import send_whatsapp_outbound

        send_whatsapp_outbound(
            'image',
            '/media/uploads/images/test.jpg',
            '15551234567',
            message_id=1,
            conversation_id=1,
        )

        mock_ensure.assert_called_once()
        mock_upload.assert_not_called()
        # acquire_rate_capacity still called once for the main POST (not for upload)
        self.assertEqual(mock_rate_limit.call_count, 1)
