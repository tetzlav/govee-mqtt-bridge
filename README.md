# Govee MQTT Bridge für ioBroker

Steuere Govee-Leuchten aus ioBroker über MQTT und die lokale Govee-LAN-Schnittstelle. Eine Bridge erkennt mehrere Leuchten. Je nach Modell kannst du sie ein- und ausschalten, Helligkeit und Farbe ändern, Szenen auswählen und einzelne RGB-Segmente steuern.

## Voraussetzungen

- Ein Linux-Rechner mit Python 3.11, dem Python-Modul `venv` und Git. Die Installation der Python-Abhängigkeiten benötigt Zugriff auf PyPI und GitHub.
- Ein erreichbarer MQTT-Broker, zum Beispiel der ioBroker-Adapter `mqtt.0` im Broker-Modus. Das MQTT-Konto braucht Lese- und Schreibzugriff auf die unten aufgeführten Topics.
- Govee-Leuchten mit Unterstützung für LAN-Steuerung. Aktiviere **LAN Control** in der Govee-App für jede Leuchte. Die Bridge sendet die Gerätesuche an UDP `239.255.255.250:4001`, empfängt Antworten auf UDP `4002` und sendet Befehle an UDP `4003` der Leuchten. Das lokale Netz und die Firewall müssen diesen Verkehr zulassen.

## Installation

```sh
git clone https://github.com/tetzlav/govee-mqtt-bridge.git
cd govee-mqtt-bridge
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## MQTT einrichten und Bridge starten

Die Bridge liest ihre Einstellungen aus der Prozessumgebung.

| Variable | Bedeutung | Standard |
| --- | --- | --- |
| `MQTT_HOST` | Hostname oder IP-Adresse des Brokers | `127.0.0.1` |
| `MQTT_PORT` | MQTT-Port des Brokers | `1883` |
| `MQTT_USERNAME` | MQTT-Benutzername | ungesetzt |
| `MQTT_PASSWORD` | MQTT-Passwort | ungesetzt |

Für den ersten Start in Bash die Broker-Adresse und den Port bei Bedarf anpassen. Wenn dein Broker eine Anmeldung verlangt, gib die Zugangsdaten bei der Abfrage ein. Bei einem Broker ohne Anmeldung beide Eingaben leer lassen.

```bash
export MQTT_HOST=127.0.0.1 MQTT_PORT=1883
read -r -p 'MQTT-Benutzername: ' MQTT_USERNAME
read -r -s -p 'MQTT-Passwort: ' MQTT_PASSWORD
printf '\n'
export MQTT_USERNAME MQTT_PASSWORD
.venv/bin/python -u bridge/govee_mqtt_bridge.py
unset MQTT_USERNAME MQTT_PASSWORD
```

Lass die Bridge laufen, bis im Terminal die gefundenen Geräte und die MQTT-Verbindungen erscheinen. Im MQTT-Adapter in ioBroker sollten dann unter `govee/` die Geräte-Topics sichtbar werden. Mit **Strg+C** beendest du den Testlauf.

Für den Dauerbetrieb unter Linux gibt es eine [systemd-Vorlage](systemd/govee-mqtt-bridge.service) und eine [MQTT-Konfigurationsvorlage](systemd/mqtt.env.example). Passe den Installationspfad, den Dienstbenutzer, die Gruppe und gegebenenfalls die Abhängigkeit von `iobroker.service` an dein System an. Die [Betriebsanleitung](docs/betrieb.md) zeigt die Einrichtung anhand einer Beispielinstallation. Lege die echte MQTT-Konfiguration mit beschränkten Dateirechten außerhalb des Repositorys ab.

## MQTT

Jedes Gerät hat eine eigene Topic-ID. Sie entspricht dem Govee-Fingerprint ohne Doppelpunkte. Die Bridge veröffentlicht erkannte Geräte unter `govee/<id>/info`.

| Topic | Zweck |
| --- | --- |
| `govee/<id>/set` | Gerätebefehle als JSON |
| `govee/<id>/segment/<n>/set` | Segmentbefehle als JSON, Segmentnummern ab 1 |
| `govee/<id>/state` | Zustand mit `on`, `brightness`, `color` und `color_temp` |
| `govee/<id>/info` | Geräteinformationen |
| `govee/<id>/capabilities` | Gerätefähigkeiten |
| `govee/<id>/available_scenes` | Szenennamen bei Geräten mit Szenenunterstützung |
| `govee/bridge/status` | Periodische Online-Meldung |

Sende aus ioBroker oder einem MQTT-Client eines dieser JSON-Objekte an das passende Topic. Ersetze `<id>` durch die ID deiner Leuchte und sende Befehle ohne Retain-Flag.

| Aktion | Topic | JSON |
| --- | --- | --- |
| Einschalten | `govee/<id>/set` | `{"on":true}` |
| Helligkeit auf 40 Prozent setzen | `govee/<id>/set` | `{"brightness":40}` |
| RGB-Farbe setzen | `govee/<id>/set` | `{"color":[255,100,0]}` |
| Farbtemperatur setzen | `govee/<id>/set` | `{"color_temp":3500}` |
| Szene auswählen | `govee/<id>/set` | `{"scene":"sunset"}` |
| Zweites Segment färben | `govee/<id>/segment/2/set` | `{"color":[255,100,0]}` |

Wähle einen Szenennamen aus `available_scenes` des jeweiligen Geräts. `color_temp` verwendet ganzzahlige Kelvinwerte von 2000 bis 6500 und hat Vorrang vor RGB im selben Befehl. Ein Statuswert von `0` steht für eine fehlende Temperaturangabe. Die früheren Eingabefelder `color_temperature`, `color_temp_mired` und `temperature` akzeptieren weiterhin Kelvinwerte.

Die Bridge initialisiert die Befehlstopics für ioBroker mit `{}` ohne Retain-Flag. Dieses leere JSON-Objekt hat keine Schaltwirkung. Beim Abonnieren erneut zugestellte Retain-Befehle werden übersprungen.

## Abhängigkeiten aktualisieren

[`requirements.txt`](requirements.txt) legt die verwendeten Versionen fest:

| Paket | Aufgabe |
| --- | --- |
| `govee-local-api` | Erkennt Govee-Geräte im LAN und sendet Befehle. Die Datei legt einen Git-Commit fest. |
| `asyncio-mqtt` | Stellt die asynchrone Verbindung zum MQTT-Broker bereit. |
| `paho-mqtt` | MQTT-Client unterhalb von `asyncio-mqtt`. Die aktuelle Bridge verwendet die in der Datei festgelegte 1.x-Version. |

Zum Aktualisieren die betreffende Versionsnummer oder bei `govee-local-api` den vollständigen Git-Commit in `requirements.txt` ändern. Prüfe Änderungen an `asyncio-mqtt` und `paho-mqtt` gemeinsam, insbesondere bei einem Wechsel der Hauptversion. Die in der Datei angegebenen Versionen sind verbindlich. Ein Aufruf mit `--upgrade` allein ändert diese Festlegungen nicht.

Die geänderte Datei in einer frischen Testumgebung installieren und prüfen:

```bash
test_venv=$(mktemp -d)
python3.11 -m venv "$test_venv"
"$test_venv/bin/python" -m pip install -r requirements.txt
"$test_venv/bin/python" -m pip check
"$test_venv/bin/python" -m unittest discover -s tests -v
```

Die Tests decken die Verarbeitung von MQTT-Befehlen mit simulierten Geräten ab. Prüfe vor einem produktiven Wechsel zusätzlich Geräteerkennung, Statusmeldungen und die benötigten Befehle mit einem Testgerät. Verwende für diesen Test einen separaten Host und einen Test-Broker, falls bereits eine Bridge läuft. So bleiben der UDP-Listener und die MQTT-Client-IDs der laufenden Bridge frei von Konflikten. Übernimm die geprüfte `requirements.txt` in Git und verwende sie für die nächste Installation.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Die Tests simulieren MQTT und Leuchten. Die [Betriebsanleitung](docs/betrieb.md#diagnose-und-rückkehr) enthält Hinweise zur Fehlersuche.
