import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from app import Monitor, Store, MediaError
from birdbuddy.feed import FeedNode


class StoreTests(unittest.TestCase):
    def test_pending_media_survives_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            (Path(tmp) / 'bird.jpg').write_bytes(b'image')
            card = {'id': 'c', 'created': '2026-01-01', 'title': 'Bird',
                    'media': [{'file': 'bird.jpg', 'video': False}]}
            store.add(card, {'telegram': False})
            store.prune(0)
            self.assertEqual(len(store.cards()), 1)
            with store.db:
                store.db.execute('UPDATE deliveries SET done=1')
            store.prune(0)
            self.assertEqual(store.cards(), [])
            self.assertEqual(store.db.execute('SELECT id FROM seen').fetchone()[0], 'c')
            self.assertFalse((Path(tmp) / 'bird.jpg').exists())
            store.db.close()


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_binary_download_checks_signature_and_cleans_rejected_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            monitor = Monitor({'email': 'test', 'password': 'test'}, store)
            for prefix, accepted in [(b'\xff\xd8\xff' + b'0' * 9, True),
                                     (b'0000ftypisom', True),
                                     (b'<html>error!', False)]:
                response = MagicMock()
                response.headers = {'Content-Type': 'application/octet-stream'}
                response.content.readexactly = AsyncMock(return_value=prefix)
                async def chunks(size):
                    yield b'rest-of-file'
                response.content.iter_chunked = chunks
                context = MagicMock()
                context.__aenter__ = AsyncMock(return_value=response)
                context.__aexit__ = AsyncMock(return_value=False)
                monitor.session = MagicMock()
                monitor.session.get.return_value = context
                target = Path(tmp) / 'download.jpg'
                if accepted:
                    await monitor.download('https://example.com', target.name)
                    self.assertEqual(target.read_bytes(), prefix + b'rest-of-file')
                    target.unlink()
                else:
                    with self.assertRaises(MediaError):
                        await monitor.download('https://example.com', target.name)
                    self.assertFalse(target.exists())
                self.assertFalse(target.with_suffix('.part').exists())
            store.db.close()

    async def test_missing_credentials_gives_actionable_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            monitor = Monitor({'email': '  ', 'password': ''}, store)
            monitor.bb.refresh = AsyncMock()
            await monitor.check()
            self.assertIn('inloggegevens ontbreken', monitor.status['error'])
            self.assertIn('herstart', monitor.status['error'])
            monitor.bb.refresh.assert_not_awaited()
            self.assertIsNone(monitor.status['last_success'])
            store.db.close()

    async def test_upstream_error_has_stage_without_secret_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            monitor = Monitor({'email': 'test', 'password': 'secret-password'}, store)
            monitor.bb.refresh = AsyncMock(side_effect=ValueError('secret-password https://signed-url'))
            with self.assertLogs('birdbuddy_addon', level='WARNING') as captured:
                await monitor.check()
            self.assertIn('aanmelden en apparaatstatus ophalen', monitor.status['error'])
            self.assertIn('ValueError', monitor.status['error'])
            output = ' '.join(captured.output) + monitor.status['error']
            self.assertNotIn('secret-password', output)
            self.assertNotIn('signed-url', output)
            self.assertIn('app.py:', output)
            store.db.close()

    async def test_retry_resumes_after_delivered_media_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'email': 'test', 'password': 'test', 'telegram': {'enabled': True},
                      'sftp': {'enabled': False}}
            store = Store(tmp)
            store.add({'id': 'c', 'created': 'today', 'title': 'Bird', 'media': [
                {'file': 'one.jpg', 'video': False}, {'file': 'two.jpg', 'video': False}]},
                {'telegram': False})
            monitor = Monitor(config, store)
            monitor.telegram = AsyncMock(side_effect=[None, OSError('offline')])
            await monitor.deliver()
            self.assertEqual(store.get('telegram:c'), '1')
            store.db.close()
            store = Store(tmp)
            monitor = Monitor(config, store)
            monitor.telegram = AsyncMock()
            await monitor.deliver()
            self.assertEqual(monitor.telegram.await_count, 1)
            self.assertEqual(monitor.telegram.call_args.args[1]['file'], 'two.jpg')
            await monitor.deliver()
            self.assertEqual(monitor.telegram.await_count, 1)
            store.db.close()

    async def test_feed_download_and_initial_delivery_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'email': 'test', 'password': 'test', 'history_days': 7,
                      'telegram': {'enabled': True, 'send_existing': False},
                      'sftp': {'enabled': False}}
            store = Store(tmp)
            monitor = Monitor(config, store)
            from birdbuddy.postcards import PostcardAnalysis
            media = {'id': 'm', '__typename': 'MediaImage', 'contentUrl': 'https://example.com/img'}
            node = FeedNode({'id': 'c', '__typename': 'FeedItemNewPostcard', 'createdAt': '2026-10-23T08:00:00Z'})
            class Client:
                feeders = {}
                refresh = AsyncMock()
                refresh_feed = AsyncMock(return_value=[node])
                identify_postcard = AsyncMock(return_value=PostcardAnalysis({'id': 'c', 'medias': [media]}))
            monitor.bb = Client()
            monitor.download = AsyncMock()
            await monitor.sync()
            self.assertEqual(len(store.cards()), 1)
            self.assertEqual(store.db.execute('SELECT done FROM deliveries').fetchone()[0], 1)
            await monitor.sync()
            self.assertEqual(monitor.download.await_count, 1)
            self.assertEqual(monitor.bb.identify_postcard.await_count, 1)
            with store.db:
                store.db.execute('DELETE FROM cards')
                store.db.execute('DELETE FROM seen')
            monitor.download = AsyncMock(side_effect=MediaError('Test downloadfout'))
            await monitor.sync()
            self.assertIn('Test downloadfout', monitor.status['error'])
            self.assertEqual(store.cards(), [])
            store.db.close()


if __name__ == '__main__':
    unittest.main()
