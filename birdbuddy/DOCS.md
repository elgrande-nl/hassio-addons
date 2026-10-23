# Configuratie

Zie [README.md](README.md) voor installatie, Telegram en SFTP.

Vul Bird Buddy-inloggegevens in bij de add-onconfiguratie. Open daarna de
webinterface vanuit Home Assistant. Telegram en SFTP staan standaard uit.

Telegram gebruikt de bestaande Home Assistant-bot en vereist toegang tot
`/share/birdbuddy` via `homeassistant.allowlist_external_dirs`.
SFTP vereist een vooraf gecontroleerd `known_hosts`-bestand.

Vanaf 0.1.7 staan er vier waarnemingen per pagina. Het eigen AppArmor-profiel
wordt door Supervisor geladen. Controleer na de update de werking en hostlogboeken;
zie de beveiligingssectie in README.md.

Open **Instellingen · ophaalplanning** in de webinterface voor elk uur ophalen
of eenmaal per dag op een vast lokaal tijdstip. De planning blijft bewaard en
werkt zonder herstart. De webinterface toont wanneer de volgende controle komt.
