import sys
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from app import Monitor, Store, MediaError, ConfigurationError, make_app
import aiohttp
from aiohttp import web
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
            for content_type, prefix, accepted in [
                    ('application/octet-stream', b'\xff\xd8\xff' + b'0' * 9, True),
                    ('text/plain', b'\xff\xd8\xff' + b'0' * 9, True),
                    ('application/binary', b'0000ftypisom', True),
                    ('image/jpeg', b'\xff\xd8\xff' + b'0' * 9, True),
                    ('application/octet-stream', b'<html>error!', False),
                    ('text/html', b'<html>error!', False),
                    ('image/jpeg', b'<html>error!', False),
                    ('application/json', b'{"error":123', False)]:
                response = MagicMock()
                response.headers = {'Content-Type': content_type}
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

    async def test_telegram_uses_configured_destination_without_birdbuddy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            monitor = Monitor({'email': '', 'password': '', 'telegram': {
                'enabled': True, 'chat_id': '-1001234', 'config_entry_id': 'entry'}}, store)
            for name in ('bird.jpg', 'bird.mp4'):
                (Path(tmp) / name).write_bytes(b'media')
            store.add({'id': 'latest', 'title': 'Koolmees', 'created': '2026-10-23',
                       'media': [{'file': 'bird.jpg', 'video': False},
                                 {'file': 'bird.mp4', 'video': True}]}, {})
            response = MagicMock()
            response.read = AsyncMock()
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=response)
            context.__aexit__ = AsyncMock(return_value=False)
            monitor.session = MagicMock(closed=False)
            monitor.session.post.return_value = context
            with patch.dict(os.environ, {'SUPERVISOR_TOKEN': 'test-token'}):
                await monitor.test_telegram()
            args, kwargs = monitor.session.post.call_args
            self.assertTrue(args[0].endswith('/telegram_bot/send_media_group'))
            self.assertEqual(kwargs['json']['chat_id'], [-1001234])
            self.assertEqual(kwargs['json']['config_entry_id'], 'entry')
            self.assertEqual([m['media_type'] for m in kwargs['json']['media']], ['photo', 'video'])
            self.assertIn('Koolmees', kwargs['json']['media'][0]['caption'])
            self.assertTrue(kwargs['json']['media'][0]['file'].endswith('/bird.jpg'))
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0], 0)
            monitor.config['telegram']['enabled'] = False
            with self.assertRaises(ConfigurationError):
                await monitor.test_telegram()
            self.assertEqual(monitor.session.post.call_count, 1)
            monitor.config['telegram']['enabled'] = True
            monitor.config['telegram']['chat_id'] = 'invalid'
            with patch.dict(os.environ, {'SUPERVISOR_TOKEN': 'test-token'}):
                with self.assertRaises(ConfigurationError):
                    await monitor.test_telegram()
            self.assertEqual(monitor.session.post.call_count, 1)
            store.db.close()

    async def test_telegram_latest_sighting_album_limits_and_single_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            monitor = Monitor({'email': '', 'password': '', 'telegram': {'enabled': True}}, store)
            monitor.telegram_call = AsyncMock()
            with self.assertRaises(ConfigurationError):
                await monitor.test_telegram()
            monitor.telegram_call.assert_not_awaited()
            store.add({'id': 'old', 'title': 'Oud', 'created': '2026-10-20',
                       'media': [{'file': 'old.jpg', 'video': False}]}, {})
            items = [{'file': str(i)+'.jpg', 'video': False} for i in range(11)]
            for item in items:
                (Path(tmp) / item['file']).write_bytes(b'photo')
            store.add({'id': 'new', 'title': 'Nieuw', 'created': '2026-10-23', 'media': items}, {})
            result = await monitor.test_telegram()
            self.assertIn('Nieuw', result)
            self.assertEqual([len(c.args[1]['media']) for c in monitor.telegram_call.call_args_list], [9, 2])
            sent = [m['file'] for c in monitor.telegram_call.call_args_list for m in c.args[1]['media']]
            self.assertEqual(len(set(sent)), 11)
            monitor.telegram_call.reset_mock()
            with store.db:
                store.db.execute('UPDATE cards SET media=? WHERE id=?', (json.dumps(items[:1]), 'new'))
            await monitor.test_telegram()
            self.assertEqual(monitor.telegram_call.call_args.args[0], 'send_photo')
            (Path(tmp) / items[0]['file']).unlink()
            monitor.telegram_call.reset_mock()
            with self.assertRaises(ConfigurationError):
                await monitor.test_telegram()
            monitor.telegram_call.assert_not_awaited()
            store.db.close()

    async def test_telegram_endpoint_reports_errors_and_requires_ui_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            monitor = Monitor({'email': '', 'password': ''}, store)
            app = make_app(monitor)
            route = next(r for r in app.router.routes()
                         if r.resource.canonical == '/api/telegram/test')
            request = MagicMock(headers={})
            monitor.test_telegram = AsyncMock(return_value='Laatste waarneming verstuurd')
            with self.assertRaises(web.HTTPForbidden):
                await route.handler(request)
            monitor.test_telegram.assert_not_awaited()
            request.headers = {'X-BirdBuddy-UI': '1'}
            response = await route.handler(request)
            self.assertTrue(json.loads(response.body)['ok'])
            monitor.test_telegram.side_effect = aiohttp.ClientResponseError(
                MagicMock(), (), status=400, message='secret', headers={})
            response = await route.handler(request)
            self.assertEqual(response.status, 502)
            self.assertIn('chat-ID', json.loads(response.body)['message'])
            self.assertNotIn('secret', response.text)
            monitor.test_telegram.side_effect = ConfigurationError('Telegram staat uit')
            response = await route.handler(request)
            self.assertEqual(response.status, 400)
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
