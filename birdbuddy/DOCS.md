# Configuratie

Zie [README.md](README.md) voor installatie, Telegram en SFTP.

Vul Bird Buddy-inloggegevens in bij de add-onconfiguratie. Open daarna de
webinterface vanuit Home Assistant. Telegram en SFTP staan standaard uit.

Telegram gebruikt de bestaande Home Assistant-bot en vereist toegang tot
`/share/birdbuddy` via `homeassistant.allowlist_external_dirs`.
SFTP vereist een vooraf gecontroleerd `known_hosts`-bestand.
