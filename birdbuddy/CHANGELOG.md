# 0.1.8

- Telegram-test verstuurt de nieuwste lokaal opgeslagen waarneming met alle foto’s en video’s.
- Meerdere bestanden worden gebundeld in albums van maximaal tien items; één bestand wordt los verstuurd.
- Duidelijke melding wanneer er nog geen media zijn of een lokaal bestand ontbreekt.

# 0.1.7

- Vier waarnemingen per pagina.
- Eigen AppArmor-profiel met beperkte bestandstoegang en zonder shelluitvoering.
- Verwachte veiligheidsscore 8 wanneer het profiel door Supervisor is geladen; validatie op de Home Assistant-host vereist.

# 0.1.6

- Eén compacte mediastapel per waarneming met vorige/volgende-knoppen en bestandsteller.
- Paginering met negen waarnemingen per pagina.
- Bewaar de gekozen mediapositie bij statusupdates en stop video’s bij bladeren.

# 0.1.5

- Knop Telegram testen in de webinterface met bevestiging en duidelijke foutmeldingen.
- De test stuurt één tekstbericht via de bestaande Home Assistant-bot, onafhankelijk van Bird Buddy-kaartjes.

# 0.1.4

- Herken mediabestanden aan de inhoud, ook bij afwijkende downloadheaders.
- Weiger foutpagina’s ook wanneer de server deze als foto of video aanbiedt.
- Meld bij niet-herkende inhoud het bekende downloadtype zonder URL of bestandsinhoud te loggen.

# 0.1.3

- Accepteer geldige foto’s en video’s met een algemeen binair downloadtype na controle van de bestandsheader.
- Toon per kaartjesfout de oorzaak en stap; log de foutlocatie zonder geheime URL’s.

# 0.1.2

- Eigen kleurenpalet met donkerblauw, paars en koraal voor de webinterface.
- Licht en donker thema, duidelijke toetsenbordfocus en contrasterende foutmeldingen.

# 0.1.1

- Duidelijke melding bij ontbrekende inloggegevens en mislukte aanmelding.
- Fouten vermelden de stap en logboeken tonen foutlocaties zonder geheime waarden.

# 0.1.0

- Eerste versie met Ingress-dashboard, apparaatstatus en lokale mediaopslag.
- Telegram via Home Assistant en optionele SFTP-upload met hostkeycontrole.
