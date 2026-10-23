"""Bird Buddy ingress dashboard and persistent media outbox."""
import asyncio
import hashlib
import json
import logging
import os
import posixpath
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from aiohttp import web
import paramiko
from birdbuddy.exceptions import AuthenticationFailedError
from birdbuddy.client import BirdBuddy
from birdbuddy.postcards import CollectedPostcard

LOG = logging.getLogger("birdbuddy_addon")
MAX_MEDIA = 100 * 1024 * 1024
MAX_TELEGRAM_BATCH = 20 * 1024 * 1024
MAX_DELIVERIES_PER_CYCLE = 4


def now():
    return datetime.now(timezone.utc).isoformat()


class ConfigurationError(ValueError):
    """A configuration problem whose message is safe to show."""


def validate_schedule(value):
    if not isinstance(value, dict) or value.get("mode") not in ("hourly", "daily"):
        raise ConfigurationError("Kies elk uur of eenmaal per dag.")
    time = value.get("time", "08:00")
    try:
        if not isinstance(time, str) or len(time) != 5 or time[2] != ":":
            raise ValueError()
        hour, minute = map(int, time.split(":"))
        if not 0 <= hour < 24 or not 0 <= minute < 60:
            raise ValueError()
        zone = value.get("timezone", "Europe/Amsterdam")
        if not isinstance(zone, str):
            raise ValueError()
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError, TypeError):
        raise ConfigurationError("Gebruik een geldig tijdstip (HH:MM) en tijdzone, bijvoorbeeld Europe/Amsterdam.") from None
    return {"mode": value["mode"], "time": time, "timezone": zone}


def next_poll(schedule, last, current):
    """Return an aware UTC deadline; persistent attempts prevent restart loops."""
    previous = datetime.fromisoformat(last).astimezone(timezone.utc) if last else None
    if schedule["mode"] == "hourly":
        return max(current, previous + timedelta(hours=1)) if previous else current
    zone = ZoneInfo(schedule["timezone"])
    local = current.astimezone(zone)
    day = local.date()
    if previous and previous.astimezone(zone).date() >= day:
        day = previous.astimezone(zone).date() + timedelta(days=1)
    hour, minute = map(int, schedule["time"].split(":"))
    # fold=0 chooses the first occurrence in autumn; UTC normalization moves
    # a nonexistent spring time forward by the DST gap.
    candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
    return max(current, candidate.astimezone(timezone.utc))


class MediaError(ValueError):
    """A media problem with a safe, user-facing explanation."""


def media_signature(data):
    return (data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a"))
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
            or data[4:8] == b"ftyp")


def error_location(exc):
    return " > ".join(f"{Path(f.filename).name}:{f.lineno} ({f.name})"
                      for f in traceback.extract_tb(exc.__traceback__))


def card_error(exc, stage):
    if isinstance(exc, MediaError):
        return str(exc)
    if isinstance(exc, aiohttp.ClientResponseError):
        return f"{stage}: server antwoordt HTTP {exc.status}"
    return f"{stage}: {type(exc).__name__}"


def sync_error(exc, stage):
    if isinstance(exc, ConfigurationError):
        return str(exc)
    if isinstance(exc, AuthenticationFailedError):
        return ("Aanmelden bij Bird Buddy mislukt. Controleer je e-mailadres en "
                "wachtwoord in de add-onconfiguratie. Gebruik een Bird Buddy-account "
                "met e-mail/wachtwoord; aanmelden via Google/Apple/Facebook werkt hier niet.")
    if isinstance(exc, (asyncio.TimeoutError, aiohttp.ClientConnectionError)):
        return f"Bird Buddy is niet bereikbaar tijdens {stage}. Controleer de internetverbinding; de add-on probeert het opnieuw."
    return f"Bird Buddy ophalen mislukt tijdens {stage} ({type(exc).__name__}). Bekijk de add-onlogboeken voor de foutlocatie."


class Store:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "cards.db")
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS cards (
            id TEXT PRIMARY KEY, created TEXT, title TEXT, media TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS deliveries (
            card TEXT, destination TEXT, done INTEGER DEFAULT 0, error TEXT,
            PRIMARY KEY(card, destination));
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
          CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY);
          INSERT OR IGNORE INTO seen SELECT id FROM cards;
        ''')

    def get(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    def cards(self):
        return [dict(r) | {"media": json.loads(r["media"])} for r in self.db.execute(
            "SELECT * FROM cards ORDER BY created DESC")]

    def add(self, card, destinations):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (card["id"],))
            self.db.execute("INSERT INTO cards VALUES (?,?,?,?)", (
                card["id"], card["created"], card["title"], json.dumps(card["media"])))
            for destination, skip in destinations.items():
                self.db.execute("INSERT INTO deliveries VALUES (?,?,?,NULL)", (
                    card["id"], destination, int(skip)))

    def prune(self, keep):
        # Unsent media stays on disk until delivery succeeds.
        for card in self.cards()[keep:]:
            pending = self.db.execute(
                "SELECT 1 FROM deliveries WHERE card=? AND done=0", (card["id"],)).fetchone()
            if pending:
                continue
            for media in card["media"]:
                (self.root / media["file"]).unlink(missing_ok=True)
            with self.db:
                self.db.execute("DELETE FROM deliveries WHERE card=?", (card["id"],))
                self.db.execute("DELETE FROM cards WHERE id=?", (card["id"],))


def sftp_upload(config, local, filename):
    """Only accept host keys supplied by the user; never trust on first use."""
    client = paramiko.SSHClient()
    client.load_host_keys(config["known_hosts"])
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(config["host"], port=config["port"], username=config["username"],
                       password=config["password"], timeout=20, auth_timeout=20,
                       banner_timeout=20, look_for_keys=False, allow_agent=False)
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(60)
            target = posixpath.join(config["directory"], filename)
            sftp.put(str(local), target + ".part")
            try:
                sftp.posix_rename(target + ".part", target)
            except OSError:
                sftp.rename(target + ".part", target)
    finally:
        client.close()


class Monitor:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.bb = BirdBuddy(config["email"], config["password"])
        self.bb.language_code = "nl"
        self.status = {"last_success": None, "error": None, "feeders": []}
        self.wake = asyncio.Event()
        self.session = None
        self.stage = "configuratie controleren"
        self.telegram_lock = asyncio.Lock()
        self.telegram_test_lock = asyncio.Lock()
        self.manual_refresh = False
        saved = self.store.get("poll_schedule")
        self.schedule = validate_schedule(json.loads(saved) if saved else config.get(
            "polling", {"mode": "hourly", "time": "08:00", "timezone": "Europe/Amsterdam"}))
        self.status["next_poll"] = None

    async def download(self, url, filename):
        target = self.store.root / filename
        temporary = target.with_suffix(".part")
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                # Signed storage URLs can advertise non-media MIME types.
                # Inspect bytes for every response, including image/video headers.
                try:
                    prefix = await response.content.readexactly(12)
                except asyncio.IncompleteReadError as exc:
                    raise MediaError("Download bevat een leeg of onvolledig mediabestand.") from exc
                if not media_signature(prefix):
                    known_types = {"text/html", "text/plain", "application/json", "application/xml",
                                   "text/xml", "application/octet-stream", "image/jpeg", "video/mp4"}
                    safe_type = content_type if content_type in known_types else "onbekend"
                    raise MediaError(
                        "Download bevat geen herkenbare JPEG-, PNG-, GIF-, WebP- of MP4-media "
                        f"(Content-Type: {safe_type}). Mogelijk een foutpagina of verlopen downloadadres")
                size = len(prefix)
                with temporary.open("wb") as output:
                    output.write(prefix)
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > MAX_MEDIA:
                            raise MediaError("Mediabestand is groter dan de limiet van 100 MiB.")
                        output.write(chunk)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    async def sync(self):
        self.stage = "configuratie controleren"
        if not str(self.config.get("email") or "").strip() or not self.config.get("password"):
            raise ConfigurationError(
                "Bird Buddy-inloggegevens ontbreken. Vul email en password in bij "
                "de add-onconfiguratie, sla op en herstart de add-on.")
        self.stage = "aanmelden en apparaatstatus ophalen"
        await self.bb.refresh()
        self.stage = "apparaatstatus verwerken"
        self.status["feeders"] = [
            {"name": f.name, "state": f.state.value, "battery": f.battery.get("percentage"),
             "charging": f.battery.get("charging"), "signal": f.signal.get("rssi"),
             "firmware": f.version} for f in self.bb.feeders.values()]
        since = datetime.now(timezone.utc) - timedelta(days=self.config["history_days"])
        self.stage = "recente kaartjes ophalen"
        cards = await self.bb.refresh_feed(since=since)
        existing = {r[0] for r in self.store.db.execute("SELECT id FROM seen")}
        destinations = {}
        for dest in ("telegram", "sftp"):
            cfg = self.config[dest]
            if cfg["enabled"]:
                first = not self.store.get("initialized_" + dest)
                destinations[dest] = first and not cfg["send_existing"]
        errors = []
        for node in reversed(cards):
            if node.node_id in existing:
                continue
            kind = node.get("__typename")
            if kind not in ("FeedItemNewPostcard", "FeedItemCollectedPostcard"):
                continue
            media = []
            try:
                card_stage = "bezoeker identificeren"
                # New feed nodes have no media until visitor identification.
                postcard = await self.bb.identify_postcard(node) if kind == "FeedItemNewPostcard" else CollectedPostcard(node)
                card_stage = "mediagegevens lezen"
                for item in postcard.medias:
                    url = item.content_url or item.thumbnail_url
                    if not url:
                        raise MediaError("Bird Buddy levert geen downloadadres voor deze media.")
                    filename = hashlib.sha256((node.node_id + item.id).encode()).hexdigest() + (
                        ".mp4" if item.is_video else ".jpg")
                    media.append({"file": filename, "video": item.is_video})
                    card_stage = "foto of video downloaden"
                    await self.download(url, filename)
                if not media:
                    raise MediaError("Bird Buddy levert nog geen media voor dit kaartje.")
                card_stage = "kaartje opslaan"
                title = ", ".join(s.name for s in postcard.species) or "Vogelbezoek"
                self.store.add({"id": node.node_id, "created": node.get("createdAt") or now(),
                                "title": title, "media": media}, destinations)
            except Exception as exc:
                detail = card_error(exc, card_stage)
                errors.append(detail)
                for item in media:
                    (self.store.root / item["file"]).unlink(missing_ok=True)
                LOG.warning("Kaartje ophalen mislukt: %s | %s", detail, error_location(exc))
        if not errors:
            for dest in destinations:
                self.store.set("initialized_" + dest, "yes")
        self.status["last_success"] = now()
        self.status["error"] = (
            f"{len(errors)} kaartje(s) konden niet worden opgehaald. "
            + "; ".join(dict.fromkeys(errors))
            + ". Volgende controle opnieuw proberen."
        ) if errors else None

    async def telegram_call(self, action, data, timeout=30):
        cfg = self.config["telegram"]
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise ConfigurationError("Home Assistant API-toegang ontbreekt. Start de add-on vanuit Home Assistant.")
        if self.session is None or self.session.closed:
            raise ConfigurationError("De add-on start nog op. Probeer het over enkele seconden opnieuw.")
        if cfg["chat_id"]:
            try:
                data["chat_id"] = [int(cfg["chat_id"])]
            except ValueError as exc:
                raise ConfigurationError("Vul bij telegram.chat_id een numeriek chat-ID in, bijvoorbeeld -1001234567890.") from exc
        if cfg["config_entry_id"]:
            data["config_entry_id"] = cfg["config_entry_id"]
        async with self.telegram_lock:
            async with self.session.post("http://supervisor/core/api/services/telegram_bot/" + action,
                 headers={"Authorization": "Bearer " + token}, json=data,
                 timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if response.status >= 400:
                    # Inspect only a bounded error body; never show or log raw HA errors.
                    body = (await response.content.read(8192)).decode("utf-8", errors="replace").lower()
                    if "allowlist" in body or "not allowed" in body or "not permitted" in body:
                        raise ConfigurationError(
                            "Home Assistant weigert toegang tot de mediabestanden. Voeg "
                            "/share/birdbuddy toe aan homeassistant.allowlist_external_dirs "
                            "in configuration.yaml en herstart Home Assistant.")
                    if "not found" in body and ("file" in body or "/share/" in body):
                        raise ConfigurationError("Home Assistant kan het mediabestand niet vinden. Controleer of /share/birdbuddy toegankelijk is vanuit Home Assistant.")
                response.raise_for_status()
                await response.read()

    def validate_telegram_media(self, media):
        size = 0
        for item in media:
            path = self.store.root / item["file"]
            if not path.is_file():
                raise ConfigurationError("Een mediabestand ontbreekt. Controleer de lokale opslag.")
            file_size = path.stat().st_size
            if not item["video"] and file_size > 10 * 1024 * 1024:
                raise ConfigurationError("Deze foto is groter dan 10 MiB. Verklein de foto voordat je die via Telegram verstuurt.")
            size += file_size
        if size > MAX_TELEGRAM_BATCH:
            raise ConfigurationError("Deze verzending is groter dan 20 MiB. De test is gestopt om het geheugengebruik van Home Assistant te beperken. Download de media eerst via de webinterface.")


    async def telegram(self, card, media):
        self.validate_telegram_media([media])
        await self.telegram_call("send_video" if media["video"] else "send_photo", {
            "file": str(self.store.root / media["file"]), "caption": card["title"],
            "parse_mode": "plain_text"})

    async def test_telegram(self):
        if self.telegram_test_lock.locked():
            raise ConfigurationError("Er loopt al een Telegram-test. Wacht tot die klaar is.")
        async with self.telegram_test_lock:
            return await self._test_telegram()

    async def _test_telegram(self):
        if not self.config["telegram"]["enabled"]:
            raise ConfigurationError("Telegram staat uit. Zet telegram.enabled aan, sla op en herstart de add-on.")
        cards = self.store.cards()
        if not cards or not cards[0]["media"]:
            raise ConfigurationError("Er is nog geen opgeslagen waarneming met media. Haal eerst kaartjes op en probeer opnieuw.")
        card = cards[0]
        self.validate_telegram_media(card["media"])
        caption = (f"Bird Buddy-test · {card['title']}\nWaarneming: {card['created']}")[:1024]
        remaining = list(card["media"])
        batches = 0
        try:
            while remaining:
                # Telegram albums contain 2–10 items. Avoid a lone final item.
                size = 9 if len(remaining) == 11 else min(10, len(remaining))
                batch, remaining = remaining[:size], remaining[size:]
                if len(batch) == 1:
                    media = batch[0]
                    await self.telegram_call("send_video" if media["video"] else "send_photo", {
                        "file": str(self.store.root / media["file"]), "caption": caption,
                        "parse_mode": "plain_text"}, timeout=120)
                else:
                    items = [{"media_type": "video" if m["video"] else "photo",
                              "file": str(self.store.root / m["file"])} for m in batch]
                    items[0]["caption"] = caption
                    await self.telegram_call("send_media_group", {
                        "media": items, "parse_mode": "plain_text"}, timeout=120)
                batches += 1
        except Exception:
            if batches:
                LOG.warning("Telegram-test gedeeltelijk verstuurd: %d groep(en). Controleer Telegram voordat je opnieuw test.", batches)
            raise
        kind = "bericht" if len(card["media"]) == 1 else f"{batches} album(s)"
        return (f"Laatste waarneming verstuurd: {card['title']} — {len(card['media'])} "
                f"bestand(en) in {kind}. Controleer je Telegram-chat of kanaal.")

    async def deliver(self):
        attempts = 0
        for card in self.store.cards():
            for row in self.store.db.execute(
                    "SELECT * FROM deliveries WHERE card=? AND done=0", (card["id"],)).fetchall():
                dest = row["destination"]
                if not self.config[dest]["enabled"]:
                    continue
                try:
                    # Per-file checkpoints avoid resending previous media on retry/restart.
                    progress_key = dest + ":" + card["id"]
                    progress = int(self.store.get(progress_key) or 0)
                    for index, media in enumerate(card["media"]):
                        if index < progress:
                            continue
                        if attempts >= MAX_DELIVERIES_PER_CYCLE:
                            return
                        attempts += 1
                        if dest == "telegram":
                            await self.telegram(card, media)
                        else:
                            await asyncio.to_thread(sftp_upload, self.config[dest],
                                                    self.store.root / media["file"], media["file"])
                        self.store.set(progress_key, str(index + 1))
                    with self.store.db:
                        self.store.db.execute("UPDATE deliveries SET done=1,error=NULL WHERE card=? AND destination=?",
                                              (card["id"], dest))
                except Exception as exc:
                    # Exception text from upstream may contain signed URLs or credentials.
                    with self.store.db:
                        self.store.db.execute("UPDATE deliveries SET error=? WHERE card=? AND destination=?",
                                              (type(exc).__name__, card["id"], dest))
                    LOG.warning("%s verzending mislukt (%s)", dest, type(exc).__name__)

    async def check(self):
        try:
            await asyncio.wait_for(self.sync(), timeout=240)
        except Exception as exc:
            self.status["error"] = sync_error(exc, self.stage)
            # Include code locations, but never upstream exception text or locals.
            locations = error_location(exc)
            LOG.warning("%s | %s", self.status["error"], locations)

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            self.session = session
            while True:
                self.wake.clear()
                current = datetime.now(timezone.utc)
                due = next_poll(self.schedule, self.store.get("last_scheduled_poll"), current)
                self.status["next_poll"] = due.isoformat()
                scheduled = False
                if not self.manual_refresh:
                    try:
                        await asyncio.wait_for(self.wake.wait(), timeout=max(0, (due-current).total_seconds()))
                    except asyncio.TimeoutError:
                        scheduled = True
                    if not scheduled and not self.manual_refresh:
                        continue  # Settings changed: recalculate without cloud traffic.
                self.manual_refresh = False
                if scheduled:
                    self.store.set("last_scheduled_poll", now())
                self.status["next_poll"] = None
                await self.check()
                await self.deliver()
                self.store.prune(self.config["keep_cards"])



async def lifecycle(app):
    task = asyncio.create_task(app["monitor"].run())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    app["monitor"].store.db.close()


def make_app(monitor):
    @web.middleware
    async def ingress_only(request, handler):
        if request.remote != "172.30.32.2":
            raise web.HTTPForbidden(text="Gebruik Home Assistant Ingress.")
        return await handler(request)

    app = web.Application(middlewares=[ingress_only])
    app["monitor"] = monitor

    async def index(request):
        return web.FileResponse(Path(__file__).with_name("index.html"))

    async def state(request):
        deliveries = [dict(r) for r in monitor.store.db.execute("SELECT * FROM deliveries")]
        return web.json_response(monitor.status | {"cards": monitor.store.cards(), "deliveries": deliveries, "polling": monitor.schedule})

    async def refresh(request):
        if request.headers.get("X-BirdBuddy-UI") != "1":
            raise web.HTTPForbidden()
        monitor.manual_refresh = True
        monitor.wake.set()
        return web.json_response({"queued": True})

    async def polling_settings(request):
        if request.headers.get("X-BirdBuddy-UI") != "1":
            raise web.HTTPForbidden()
        try:
            schedule = validate_schedule(await request.json())
        except (ConfigurationError, ValueError) as exc:
            message = str(exc) if isinstance(exc, ConfigurationError) else "Ongeldige instellingen."
            return web.json_response({"ok": False, "message": message}, status=400)
        monitor.store.set("poll_schedule", json.dumps(schedule))
        monitor.schedule = schedule
        monitor.wake.set()
        return web.json_response({"ok": True, "message": "Planning opgeslagen.", "polling": schedule})

    async def telegram_test(request):
        if request.headers.get("X-BirdBuddy-UI") != "1":
            raise web.HTTPForbidden()
        try:
            result = await monitor.test_telegram()
        except ConfigurationError as exc:
            return web.json_response({"ok": False, "message": str(exc)}, status=400)
        except aiohttp.ClientResponseError as exc:
            hints = {
                400: "Controleer het chat-ID, de toegestane chats, het config-entry-ID en allowlist_external_dirs voor /share/birdbuddy.",
                401: "Controleer de Home Assistant API-toegang en Telegram-integratie.",
                403: "Controleer de rechten van de bot en de toegestane chats.",
                404: "Controleer of de Telegram bot-integratie in Home Assistant geladen is.",
            }
            message = (f"Home Assistant weigert de Telegram-test (HTTP {exc.status}). "
                       + hints.get(exc.status, "Controleer de Telegram-integratie en Home Assistant-logboeken."))
            message += " Mogelijk is een deel al verstuurd; controleer Telegram voordat je opnieuw test."
            return web.json_response({"ok": False, "message": message}, status=502)
        except (asyncio.TimeoutError, aiohttp.ClientConnectionError):
            return web.json_response({"ok": False, "message":
                "Geen bevestiging van Home Assistant ontvangen. Controleer eerst Telegram voordat je opnieuw test; het bericht kan toch zijn verstuurd."}, status=504)
        except Exception as exc:
            LOG.warning("Telegram-test mislukt (%s) | %s", type(exc).__name__, error_location(exc))
            return web.json_response({"ok": False, "message":
                "Telegram-test mislukt. Controleer de Home Assistant-logboeken."}, status=502)
        return web.json_response({"ok": True, "message": result})

    async def media(request):
        name = request.match_info["name"]
        allowed = {m["file"] for c in monitor.store.cards() for m in c["media"]}
        if name not in allowed:
            raise web.HTTPNotFound()
        return web.FileResponse(monitor.store.root / name)

    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/refresh", refresh)
    app.router.add_post("/api/settings/polling", polling_settings)
    app.router.add_post("/api/telegram/test", telegram_test)
    app.router.add_get("/media/{name}", media)
    app.cleanup_ctx.append(lifecycle)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    LOG.info("Bird Buddy add-on 0.2.0 gestart (PID %d)", os.getpid())
    config = json.loads(Path("/data/options.json").read_text())
    store = Store("/share/birdbuddy")
    web.run_app(make_app(Monitor(config, store)), host="0.0.0.0", port=8099, access_log=None)
