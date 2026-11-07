# Govee Local MQTT Bridge für ioBroker

Die Bridge verbindet den MQTT-Adapter in ioBroker mit Govee-Leuchten im lokalen Netz. Aktiviere für jede Leuchte die LAN-Steuerung. Die Bridge erkennt mehrere Geräte und verwendet pro Gerät dessen Fingerprint ohne Doppelpunkte als Topic-ID.

## Stand auf dem ioBroker-Host

Das Git-Repository liegt unter `/opt/govee-bridge`. Die versionierte Fassung steht in `bridge/govee_mqtt_bridge.py`. Der produktive Dienst `govee-mqtt-bridge.service` verwendet weiterhin das bestehende Skript im Projektstamm und die dort installierte Python-Umgebung. Die Aktivierung der versionierten Fassung ist ein separater Schritt.

## Entwicklung und Tests

Für eine frische Entwicklungsumgebung mit Python 3.11 und Git:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Auf dem ioBroker-Host lassen sich die Tests aus dem Projektordner auch mit `./bin/python -m unittest discover -s tests -v` ausführen. Die Tests simulieren MQTT und Leuchten.

Die versionierte Bridge liest `MQTT_USERNAME` und `MQTT_PASSWORD` aus der Prozessumgebung. `MQTT_HOST` und `MQTT_PORT` sind optional und verwenden standardmäßig `127.0.0.1` und `1883`. Zugangsdaten gehören in die private Dienstumgebung oder in eine zugriffsgeschützte Konfigurationsdatei außerhalb von Git. Die Bridge lädt keine dotenv-Datei automatisch.

## MQTT

| Topic | Zweck |
| --- | --- |
| `govee/<id>/set` | Gerätebefehle als JSON |
| `govee/<id>/segment/<n>/set` | Segmentbefehle als JSON, Segmentnummern ab 1 |
| `govee/<id>/state` | Zustand mit `on`, `brightness`, `color` und `color_temp` |
| `govee/<id>/info` | Geräteinformationen |
| `govee/<id>/capabilities` | Gerätefähigkeiten |
| `govee/<id>/available_scenes` | Szenennamen, sofern das Gerät Szenen unterstützt |
| `govee/bridge/status` | Periodische Online-Meldung |

Beispielbefehle sind `{"on":true}`, `{"brightness":40}`, `{"color":[255,100,0]}`, `{"color_temp":3500}` und `{"scene":"sunset"}`. Verwende einen Szenennamen aus `available_scenes`. Die Bibliothek löst den Namen selbst auf.

`color_temp` verwendet ganzzahlige Kelvinwerte von 2000 bis 6500 und hat Vorrang vor RGB im selben Befehl. Die früheren Eingabeschlüssel `color_temperature`, `color_temp_mired` und `temperature` akzeptieren weiterhin Kelvinwerte. In den Ausgaben heißt das Feld `color_temp`. Ein gemeldeter Wert von 0 ist der bisherige Platzhalter ohne Temperaturwert.

Die Bridge initialisiert die Befehls-Topics für ioBroker mit `{}` ohne Retain-Flag. Leere JSON-Objekte haben keine Schaltwirkung. Beim Abonnieren vom Broker wiedergegebene Retain-Nachrichten werden übersprungen. Sende Schaltbefehle ohne Retain-Flag.
