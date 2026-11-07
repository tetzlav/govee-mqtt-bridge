# Betrieb der Govee Local MQTT Bridge

## Voraussetzungen

- Auf dem ioBroker-Host sind Debian 12, Python 3.11 und Git im Einsatz. Für eine Neuinstallation unter Debian 12 werden die Pakete `python3`, `python3-venv` und `git` benötigt. Die Installation der festgelegten Python-Pakete benötigt Zugriff auf PyPI und GitHub.
- Die Unit-Vorlage ist auf `/opt/govee-bridge`, den Dienstbenutzer und die Gruppe `as` sowie `iobroker.service` zugeschnitten. Auf anderen Hosts diese Angaben anpassen. Der Dienstbenutzer braucht Lese- und Ausführungsrechte für Projekt und Python-Umgebung.
- Der ioBroker-Adapter `mqtt.0` stellt auf diesem Host den Broker bereit. Die Bridge verbindet sich mit `127.0.0.1:1883`. Das MQTT-Konto muss `govee/+/set` und `govee/+/segment/+/set` abonnieren und unter `govee/` veröffentlichen dürfen. Bei einem anderen Broker dessen Adresse und die systemd-Abhängigkeit entsprechend anpassen.
- Die Leuchten müssen die Govee-LAN-Steuerung unterstützen. Aktiviere diese pro Leuchte in der Govee-App. Die Bridge sendet Discovery-Pakete per UDP an `239.255.255.250:4001`, empfängt Antworten und Status auf UDP-Port `4002` und sendet Befehle an UDP-Port `4003` der Leuchten. Host, Leuchten und etwaige VLAN- oder Firewall-Regeln müssen diesen Verkehr zulassen. Pro Host sollte nur eine Instanz UDP-Port 4002 belegen.

## Versionierte Fassung vorbereiten

Das Repository muss unter `/opt/govee-bridge` vorliegen. Dort als Projektbenutzer ausführen:

```sh
cd /opt/govee-bridge
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m unittest discover -s tests -v
```

Die separate `.venv` lässt sich aus `requirements.txt` wiederherstellen. Die Tests verbinden sich weder mit dem Broker noch mit Lampen.

Für die Aktivierung wird eine private Konfigurationsdatei benötigt. Bei einer Ersteinrichtung die Vorlage kopieren und auf dem Host bearbeiten. Bei einer bereits vorhandenen Datei direkt mit `sudoedit` arbeiten.

```sh
sudo install -d -o root -g root -m 0750 /etc/govee-bridge
sudo test -e /etc/govee-bridge/mqtt.env ||
  sudo install -o root -g root -m 0600 systemd/mqtt.env.example /etc/govee-bridge/mqtt.env
sudoedit /etc/govee-bridge/mqtt.env
```

In der Kopie `MQTT_USERNAME` und `MQTT_PASSWORD` setzen und die Kommentarzeichen davor entfernen. Die Datei verwendet die Syntax von systemd `EnvironmentFile`. Bei Werten mit Leer- oder Sonderzeichen die dort geltenden Anführungsregeln beachten. Zugangsdaten aus dem ioBroker-MQTT-Adapter übernehmen und außerhalb des Repositorys aufbewahren.

## Aktivierung nach Freigabe

Vor dem Wechsel bei der bestehenden Installation die Unit sichern und den ausgegebenen Sicherungspfad für die Rückkehr aufheben. Das bisherige Skript und seine Python-Umgebung im Projektstamm werden für die Rückkehr weiter benötigt.

```sh
cd /opt/govee-bridge
backup_dir=$(sudo mktemp -d /root/govee-bridge-systemd.XXXXXXXX)
sudo cp -p /etc/systemd/system/govee-mqtt-bridge.service "$backup_dir/"
printf '%s\n' "$backup_dir"
```

Vorlage prüfen, installieren und den Dienst umstellen. Bei einer Neuinstallation gibt es keine bisherige Unit zu sichern. Die bestehende Installation startet die Bridge bereits automatisch. Bei einer Neuinstallation den Dienst zusätzlich mit `sudo systemctl enable govee-mqtt-bridge.service` für den Systemstart aktivieren.

```sh
sudo systemd-analyze verify "$PWD/systemd/govee-mqtt-bridge.service"
sudo install -m 0644 systemd/govee-mqtt-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart govee-mqtt-bridge.service
sudo systemctl status govee-mqtt-bridge.service --no-pager
sudo journalctl -u govee-mqtt-bridge.service -n 80 --no-pager
```

Im Journal auf Geräteerkennung und beide MQTT-Verbindungen achten. In ioBroker die `info`- und `state`-Topics je Gerät prüfen. Ein aktiver systemd-Dienst allein zeigt noch keine erfolgreiche Geräteerkennung an. Schaltbefehle an produktive Leuchten nur gezielt auslösen. Für ioBroker-Skripte und Visualisierungen ist das Feld im Zustand `color_temp` in Kelvin. Die [MQTT-Schnittstelle](../README.md#mqtt) beschreibt Topics und Befehle.

## Diagnose und Rückkehr

| Symptom | Prüfen |
| --- | --- |
| Dienst startet nicht | Journal, Pfade der Unit, Dienstbenutzer, Python-Abhängigkeiten und Existenz der MQTT-Konfigurationsdatei |
| Keine Geräte gefunden | LAN-Steuerung, Multicast und UDP-Ports sowie eine eventuell parallel laufende Govee-Integration auf dem Host |
| Geräte gefunden, MQTT fehlt | Aktiven ioBroker-MQTT-Adapter, Adresse und Port, Zugangsdaten und Topic-Berechtigungen |
| Einzelne Befehle ohne Wirkung | Geräte-ID, Segmentnummer, JSON, Retain-Flag und für Szenen die Liste `available_scenes` |

Für die Rückkehr zur bisherigen Installation nach Freigabe den zuvor ausgegebenen Sicherungspfad einsetzen, die Unit wiederherstellen und den Dienst neu starten:

```sh
sudo install -m 0644 /root/govee-bridge-systemd.XXXXXXXX/govee-mqtt-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart govee-mqtt-bridge.service
sudo systemctl status govee-mqtt-bridge.service --no-pager
```
