#!/usr/bin/env python3
import asyncio
import json
import logging
import os
from typing import Dict, List, Tuple, Optional
from datetime import datetime

from asyncio_mqtt import Client as MQTTClient, MqttError
from govee_local_api import GoveeController, GoveeDevice

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL = logging.INFO
logging.basicConfig(level=LOG_LEVEL)

for handler in logging.getLogger().handlers:
    handler.setFormatter(
        logging.Formatter(
            fmt="[{asctime}] {levelname}:{name}: {message}",
            style="{",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

# Govee-Bibliothek standardmäßig etwas leiser machen
logging.getLogger("govee_local_api").setLevel(logging.INFO)

LOGGER = logging.getLogger("govee_mqtt_bridge")

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
MQTT_PREFIX = "govee"

# Zeitintervall für erzwungenen State-Republish/Healthcheck in Sekunden
STATE_INTERVAL = 300
ALLOWED_DEVICES: Optional[List[str]] = None

# Globaler MQTT-Client für den Update-Callback
MQTT_PUBLISH_CLIENT: Optional[MQTTClient] = None

# Globaler State-Cache: Speichert den zuletzt erfolgreich gepublishten State
# Format: {"<dev_id>": {"on": True, "brightness": 50, ...}, ...}
LAST_PUBLISHED_STATE: Dict[str, Dict] = {}


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _device_id(device: GoveeDevice) -> str:
    """Normierte ID für MQTT Topics (Fingerprint ohne Doppelpunkte)."""
    try:
        return device.fingerprint.replace(":", "")
    except AttributeError:
        try:
            return device.ip
        except AttributeError:
            return str(device)


def _iter_devices(controller: GoveeController) -> List[GoveeDevice]:
    """Geräte unabhängig von interner Struktur (dict/list) liefern."""
    devs = controller.devices
    if isinstance(devs, dict):
        return list(devs.values())
    return list(devs)


def _find_device(controller: GoveeController, dev_id: str) -> Optional[GoveeDevice]:
    """Gerät anhand unseres device_id() finden."""
    for dev in _iter_devices(controller):
        if _device_id(dev) == dev_id:
            return dev
    return None


def _extract_caps(device: GoveeDevice) -> Tuple[Optional[object], Optional[str]]:
    """
    Versucht, ein Capabilities-/Features-Objekt am Device zu finden.
    """
    candidate_names = [
        "capabilities",
        "light_capabilities",
        "features",
        "_capabilities",
        "_light_capabilities",
    ]
    for name in candidate_names:
        if hasattr(device, name):
            obj = getattr(device, name)
            if obj is not None:
                LOGGER.debug(
                    "Found capabilities on %s via attribute '%s': %r",
                    _device_id(device),
                    name,
                    obj,
                )
                return obj, name

    # Heuristik über Typnamen / Modulnamen
    for name in dir(device):
        if name.startswith("_"):
            continue
        try:
            obj = getattr(device, name)
        except Exception:
            continue
        cls = type(obj)
        mod = getattr(cls, "__module__", "").lower()
        cls_name = cls.__name__.lower()
        if "light_capabilities" in mod or cls_name.endswith("capabilities"):
            LOGGER.debug(
                "Heuristically detected capabilities on %s via attribute '%s': %r",
                _device_id(device),
                name,
                obj,
            )
            return obj, name

    LOGGER.debug("No capabilities structure found on %s", _device_id(device))
    return None, None


def _normalize_value(value):
    """Konvertiert nicht-JSON-kompatible Typen in etwas Serialisierbares."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return repr(value)
    if hasattr(value, "name"):
        # z.B. Enum-Mitglieder
        return value.name
    return value


def _to_plain_dict(obj: object) -> Dict:
    """
    Konvertiert ein beliebiges Objekt in ein dict und normalisiert Werte.
    """

    if obj is None:
        return {}

    def _recursively_normalize(data):
        if isinstance(data, dict):
            return {k: _recursively_normalize(v) for k, v in data.items()}
        if isinstance(data, list):
            return [_recursively_normalize(item) for item in data]
        if isinstance(data, tuple):
            # Tuples zu Listen machen, damit es in JSON sauber ist
            return [_recursively_normalize(item) for item in data]
        return _normalize_value(data)

    d: Dict = {}

    # 1) Falls das Objekt selbst eine as_dict()-Methode hat
    if hasattr(obj, "as_dict") and callable(getattr(obj, "as_dict")):
        try:
            d = obj.as_dict()
        except Exception as e:
            LOGGER.warning("as_dict() on %r failed: %s", obj, e)

    # 2) Fallback auf __dict__
    if not d and hasattr(obj, "__dict__"):
        d = {
            k: v
            for k, v in vars(obj).items()
            if not k.startswith("_") and not callable(v)
        }

    # 3) Direktes dict
    if not d and isinstance(obj, dict):
        d = obj

    if not d:
        # Als Notnagel einfach repr(obj) exportieren
        return {"value": repr(obj)}

    return _recursively_normalize(d)


def _extract_info(device: GoveeDevice) -> Dict:
    """
    Baut ein generisches info-Dict, inkl. Segmentanzahl.
    """

    info_obj = None
    for name in ("info", "device_info", "_info", "_device_info"):
        if hasattr(device, name):
            info_obj = getattr(device, name)
            if info_obj is not None:
                LOGGER.debug(
                    "Found info object on %s via '%s': %r",
                    _device_id(device),
                    name,
                    info_obj,
                )
                break

    base: Dict = {}

    # Basisinfos aus device.as_dict(), sofern vorhanden
    if hasattr(device, "as_dict") and callable(getattr(device, "as_dict")):
        try:
            base = _to_plain_dict(device.as_dict())
        except Exception as e:
            LOGGER.warning("device.as_dict() failed on %s: %s", device, e)

    # Minimalinformationen ergänzen
    try:
        base.setdefault("ip", device.ip)
    except AttributeError:
        pass
    try:
        base.setdefault("fingerprint", device.fingerprint)
    except AttributeError:
        pass
    try:
        base.setdefault("sku", device.sku)
    except AttributeError:
        pass

    # Info-Objekt mit hineinmischen
    if info_obj is not None:
        extra = _to_plain_dict(info_obj)
        base.update(extra)

    # Segmentanzahl
    segment_count = None

    # 1) Direkt am Device
    for attr in ("segment_count", "num_segments"):
        if hasattr(device, attr):
            try:
                val = getattr(device, attr)
                if isinstance(val, int) and val > 0:
                    segment_count = val
                    LOGGER.debug(
                        "Found segment count on device directly: %d", segment_count
                    )
                    break
            except Exception:
                continue

    # 2) Aus Capabilities
    if segment_count is None:
        caps, _ = _extract_caps(device)
        if caps:
            caps_dict = _to_plain_dict(caps)
            for key in ("segment_count", "segments", "num_segments"):
                val = caps_dict.get(key)
                if isinstance(val, int) and val > 0:
                    segment_count = val
                    LOGGER.debug(
                        "Found segment count (int) in capabilities: %d", segment_count
                    )
                    break
                elif isinstance(val, list) and len(val) > 0:
                    segment_count = len(val)
                    LOGGER.debug(
                        "Found segment count (list length) in capabilities: %d",
                        segment_count,
                    )
                    break

    if segment_count is not None:
        base["segment_count"] = segment_count

    if "colorTemperature" in base:
        base["color_temp"] = base.pop("colorTemperature")

    return base


def _build_device_state(device: GoveeDevice) -> Dict:
    """Erzeugt den kanonischen State-Payload zum Vergleichen/Publishen."""
    d: Dict = {}
    if hasattr(device, "as_dict") and callable(getattr(device, "as_dict")):
        try:
            d = device.as_dict()
        except Exception as e:
            LOGGER.warning("device.as_dict() failed on %s: %s", device, e)

    color = d.get("color")
    if isinstance(color, tuple):
        color = list(color)

    # Die Bibliothek liefert die Farbtemperatur in Kelvin.
    temperature = d.get("colorTemperature", d.get("temperature", 0))

    # Wichtig: Wir garantieren die Reihenfolge und den Typ der Schlüssel
    payload = {
        "on": bool(d.get("on")),
        "brightness": int(d.get("brightness", 0)),
        "color": color or [0, 0, 0],
        "color_temp": int(temperature),
    }

    return payload


def update_device_callback(device: GoveeDevice) -> None:
    """Wird aufgerufen, wenn der Controller Status-Updates vom Gerät erhält."""
    LOGGER.debug("Device updated by Govee controller: %s", device)

    if MQTT_PUBLISH_CLIENT:
        try:
            loop = asyncio.get_running_loop()
            # Keine force_publish=True, Diffing in publish_device_state übernimmt
            loop.create_task(publish_device_state(MQTT_PUBLISH_CLIENT, device))
        except RuntimeError as e:
            LOGGER.warning(
                "Could not schedule state publish for %s: %s", _device_id(device), e
            )


def discovered_callback(device: GoveeDevice, is_new: bool) -> bool:
    """Callback bei Geräteerkennung."""
    LOGGER.info("Discovered device: %s (new=%s)", device, is_new)

    caps, caps_name = _extract_caps(device)
    LOGGER.debug(
        "Capabilities candidate on %s: attr=%r, value=%r",
        _device_id(device),
        caps_name,
        caps,
    )

    if is_new:
        device.set_update_callback(update_device_callback)

        # NEU: Trigger für /set-Topics beim ersten Entdecken auslösen
        if MQTT_PUBLISH_CLIENT is not None:
            try:
                loop = asyncio.get_running_loop()
                # Task zum Publizieren der Info/Caps/Trigger starten (nicht den State, der kommt später)
                loop.create_task(_publish_set_trigger(MQTT_PUBLISH_CLIENT, device))
                loop.create_task(_publish_capabilities(MQTT_PUBLISH_CLIENT, device))
                loop.create_task(_publish_info(MQTT_PUBLISH_CLIENT, device))
                LOGGER.info("Scheduled set topic trigger for new device %s", _device_id(device))
            except RuntimeError as e:
                LOGGER.warning(
                    "Could not schedule initial publish for %s (loop error): %s", _device_id(device), e
                )
        else:
            LOGGER.warning("MQTT_PUBLISH_CLIENT is not set. Cannot publish set trigger for new device %s", _device_id(device))

    return True


# ---------------------------------------------------------------------------
# MQTT-Publishing
# ---------------------------------------------------------------------------


async def _publish_capabilities(mqtt: MQTTClient, device: GoveeDevice) -> None:
    dev_id = _device_id(device)
    caps, attr_name = _extract_caps(device)
    if caps is None:
        LOGGER.info("No capabilities found for %s", dev_id)
        return

    caps_dict = _to_plain_dict(caps)
    topic_caps = f"{MQTT_PREFIX}/{dev_id}/capabilities"

    # Vollständige Capabilities publizieren
    await mqtt.publish(topic_caps, json.dumps(caps_dict), qos=0, retain=True)
    LOGGER.info(
        "Published capabilities for %s (via '%s') to %s",
        dev_id,
        attr_name,
        topic_caps,
    )

    # Liste der verfügbaren Szenen publizieren
    scenes_raw = caps_dict.get("scenes")
    if isinstance(scenes_raw, dict) and scenes_raw:
        scene_names = sorted(scenes_raw.keys())
        topic_scenes = f"{MQTT_PREFIX}/{dev_id}/available_scenes"
        await mqtt.publish(topic_scenes, json.dumps(scene_names), qos=0, retain=True)
        LOGGER.info(
            "Published available scenes for %s to %s: %s",
            dev_id,
            topic_scenes,
            scene_names,
        )
    else:
        LOGGER.debug("No scenes found in capabilities for %s", dev_id)


async def _publish_info(mqtt: MQTTClient, device: GoveeDevice) -> None:
    dev_id = _device_id(device)
    info_dict = _extract_info(device)
    topic = f"{MQTT_PREFIX}/{dev_id}/info"
    await mqtt.publish(topic, json.dumps(info_dict), qos=0, retain=True)
    LOGGER.info("Published info for %s to %s", dev_id, topic)


async def _publish_set_trigger(mqtt: MQTTClient, device: GoveeDevice) -> None:
    """Legt ioBroker-Objekte mit einem nicht schaltenden JSON-Objekt an."""
    prefix = f"{MQTT_PREFIX}/{_device_id(device)}"
    segment_count = _extract_info(device).get("segment_count", 0)
    topics = [f"{prefix}/set"]
    topics.extend(f"{prefix}/segment/{index}/set" for index in range(1, segment_count + 1))
    for topic in topics:
        await mqtt.publish(topic, "{}", qos=0, retain=False)


async def publish_device_state(mqtt: MQTTClient, device: GoveeDevice, force_publish: bool = False) -> None:
    """
    State + Capabilities + Info publizieren.
    Veröffentlicht nur den State, wenn er sich geändert hat ODER force_publish=True.
    """
    dev_id = _device_id(device)

    # ------------------------------------------------------------
    # 0) State erzeugen und vergleichen
    # ------------------------------------------------------------
    new_state_payload = _build_device_state(device)
    last_state_payload = LAST_PUBLISHED_STATE.get(dev_id)

    is_state_changed = new_state_payload != last_state_payload

    if not force_publish and not is_state_changed:
        # Loggen nur beim Update-Callback (nicht beim Health-Loop)
        if not asyncio.current_task().get_name().startswith("health_and_republish_loop"):
            LOGGER.debug("State for %s unchanged. Skipping publish.", dev_id)
        return

    # ------------------------------------------------------------
    # 1) State publizieren
    # ------------------------------------------------------------
    topic = f"{MQTT_PREFIX}/{dev_id}/state"
    await mqtt.publish(topic, json.dumps(new_state_payload), qos=0, retain=True)
    LOGGER.info(
        "Published state for %s (%s): %s",
        dev_id,
        "FORCED" if force_publish else "CHANGED",
        new_state_payload
    )

    # Zustand speichern
    LAST_PUBLISHED_STATE[dev_id] = new_state_payload

    # ------------------------------------------------------------
    # 2) Capabilities und Info immer (re-)publizieren (diese ändern sich selten)
    # ------------------------------------------------------------
    try:
        await _publish_capabilities(mqtt, device)
    except Exception as e:
        LOGGER.exception("Error publishing capabilities for %s: %s", dev_id, e)

    try:
        await _publish_info(mqtt, device)
    except Exception as e:
        LOGGER.exception("Error publishing info for %s: %s", dev_id, e)


# ---------------------------------------------------------------------------
# Command Handling
# ---------------------------------------------------------------------------


async def _apply_device_command(device: GoveeDevice, data: Dict) -> None:
    dev_id = _device_id(device)

    # Flag, ob das Gerät ausgeschaltet werden soll (für die Priorisierung)
    should_turn_off = False

    # ------------------------------------------------------------
    # 1) Szene zuerst (Wenn Szene gesetzt, dann sofort RETURN)
    # ------------------------------------------------------------
    if "scene" in data:
        scene_name = data["scene"]
        if not isinstance(scene_name, str) or not scene_name.strip() or scene_name.lower() == "none":
            return

        await device.turn_on()
        await device.set_scene(scene_name)
        LOGGER.info("Device %s: set scene=%r", dev_id, scene_name)
        return

    # ------------------------------------------------------------
    # 2) On/Off (Priorität: Ausschalten beendet den Befehlsfluss)
    # ------------------------------------------------------------
    if "on" in data:
        if data["on"]:
            await device.turn_on()
        else:
            await device.turn_off()
            should_turn_off = True # Flag setzen, um weitere Befehle zu blockieren
        LOGGER.info("Device %s: set on=%s", dev_id, data["on"])

    # Wenn wir ausgeschaltet haben, ignorieren wir alle weiteren Befehle.
    if should_turn_off:
        LOGGER.info("Device %s: Turn off requested. Ignoring subsequent commands in payload.", dev_id)
        return # Funktion beenden

    # ------------------------------------------------------------
    # 3) Standard-Kommandos (Farbe/Helligkeit)
    # ------------------------------------------------------------

    # Helligkeit (0–100) (Kann immer parallel zu Farbmodi gesetzt werden)
    if "brightness" in data:
        value = int(data["brightness"])
        await device.set_brightness(value)
        LOGGER.info("Device %s: set brightness=%s", dev_id, value)

    # Farbtemperatur HAT VORRANG VOR RGB, falls beides im Payload ist
    color_mode_set = False

    for key in ("color_temp", "color_temperature", "color_temp_mired", "temperature"):
        if key not in data:
            continue
        try:
            raw_temp = data[key]
            temp = int(raw_temp)
            if isinstance(raw_temp, float) and not raw_temp.is_integer():
                raise ValueError("Kelvin muss ganzzahlig sein")
        except (TypeError, ValueError, OverflowError):
            LOGGER.warning("Device %s: invalid color_temp", dev_id)
            return
        if not 2000 <= temp <= 6500:
            LOGGER.warning("Device %s: color_temp outside 2000–6500 K", dev_id)
            return
        await device.set_temperature(temp)
        LOGGER.info("Device %s: set color_temp=%s K", dev_id, temp)
        color_mode_set = True
        break

    # B) RGB-Farbe (NUR, wenn Farbtemperatur NICHT gesetzt wurde)
    if not color_mode_set and "color" in data:
        color = data["color"]
        if isinstance(color, list) and len(color) == 3:
            r, g, b = color
            await device.set_rgb_color(r, g, b)
            LOGGER.info("Device %s: set rgb_color=%s", dev_id, color)
        else:
            LOGGER.warning("Device %s: invalid color payload: %r", dev_id, color)


async def _apply_segment_command(
    device: GoveeDevice,
    seg_idx: int,
    data: Dict,
) -> None:
    """Steuert einzelne Segmente (z.B. für Lightstrips mit mehreren Zonen)."""
    dev_id = _device_id(device)

    if "on" in data:
        if data["on"]:
            LOGGER.debug(
                "Device %s: segment %d on (no-op, color controls it)",
                dev_id,
                seg_idx,
            )
        else:
            try:
                await device.turn_segment_off(seg_idx)
                LOGGER.debug("Device %s: segment %d turned off", dev_id, seg_idx)
            except AttributeError:
                LOGGER.warning(
                    "Device %s: no turn_segment_off() for segments",
                    dev_id,
                )

    if "color" in data:
        color = data["color"]
        if isinstance(color, list) and len(color) == 3:
            r, g, b = color
            try:
                await device.set_segment_rgb_color(seg_idx, r, g, b)
                LOGGER.debug(
                    "Device %s: segment %d set rgb_color=%s",
                    dev_id,
                    seg_idx,
                    color,
                )
            except AttributeError:
                LOGGER.warning(
                    "Device %s: no set_segment_rgb_color() for segments",
                    dev_id,
                )
        else:
            LOGGER.warning(
                "Device %s: invalid segment color payload: %r", dev_id, color
            )

    if "brightness" in data:
        LOGGER.warning(
            "Device %s: segment brightness not supported (ignoring)",
            dev_id,
        )


async def handle_command(
    controller: GoveeController,
    mqtt: MQTTClient,
    topic: str,
    payload: str,
) -> None:
    """
    Verarbeitet MQTT-Kommandos auf Topics:
      govee/<dev_id>/set
      govee/<dev_id>/segment/<n>/set
    """
    parts = topic.split("/")

    # Erwartet: govee/<dev_id>/...
    if len(parts) < 3 or parts[0] != MQTT_PREFIX:
        LOGGER.debug("Ignoring MQTT topic (not our prefix): %s", topic)
        return

    dev_id = parts[1]
    path = parts[2:]

    device = _find_device(controller, dev_id)
    if device is None:
        LOGGER.warning("Unknown device %s for topic %s", dev_id, topic)
        return

    # Logging erst, nachdem das Gerät als bekannt identifiziert wurde
    LOGGER.info(
        "MQTT command received for %s on %s: %s",
        dev_id,
        topic,
        payload,
    )

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        LOGGER.warning("Invalid JSON on %s: %s", topic, payload)
        return

    if not isinstance(data, dict) or not data:
        return

    if path == ["set"]:
        await _apply_device_command(device, data)
        await publish_device_state(mqtt, device) # Kein Force, nutzt Diffing
        return

    if len(path) == 3 and path[0] == "segment" and path[2] == "set":
        try:
            seg_idx = int(path[1])
        except ValueError:
            LOGGER.warning("Invalid segment index in topic %s", topic)
            return

        await _apply_segment_command(device, seg_idx, data)
        await publish_device_state(mqtt, device) # Kein Force, nutzt Diffing
        return

    LOGGER.debug("Unhandled topic path %s for %s", path, dev_id)


# ---------------------------------------------------------------------------
# Haupt-Loops
# ---------------------------------------------------------------------------

async def mqtt_command_loop(controller: GoveeController) -> None:
    """Hört auf govee/+/set und govee/+/segment/+/set und ruft handle_command()."""
    while True:
        try:
            kwargs = {"client_id": "govee-bridge-cmd"}
            if MQTT_USERNAME:
                kwargs["username"] = MQTT_USERNAME
            if MQTT_PASSWORD:
                kwargs["password"] = MQTT_PASSWORD

            LOGGER.info("Connecting to MQTT for command loop...")
            async with MQTTClient(MQTT_HOST, MQTT_PORT, **kwargs) as mqtt:
                LOGGER.info(
                    "Connected to MQTT %s:%s for commands", MQTT_HOST, MQTT_PORT
                )

                async with mqtt.messages() as messages:
                    await mqtt.subscribe(f"{MQTT_PREFIX}/+/set")
                    await mqtt.subscribe(f"{MQTT_PREFIX}/+/segment/+/set")
                    LOGGER.info("Subscribed to command topics")
                    async for msg in messages:
                        if msg.retain:
                            continue
                        topic = str(msg.topic)
                        payload = msg.payload.decode(errors="ignore")
                        try:
                            await handle_command(controller, mqtt, topic, payload)
                        except MqttError:
                            raise
                        except (TypeError, ValueError, OverflowError):
                            LOGGER.warning("Invalid command on %s", topic)

        except MqttError as e:
            LOGGER.error("MQTT error in command loop: %s, reconnecting in 5s", e)
            await asyncio.sleep(5)


async def initial_state_publish(mqtt: MQTTClient, controller: GoveeController) -> None:
    """
    Beim Start einmal alle bekannten Geräte:
      - aktuellen State publizieren
      - /set-Topics triggern (damit ioBroker Topics anlegt)
    """
    LOGGER.info(
        "Starting initial state publish and set topic triggering for all devices..."
    )

    for dev in _iter_devices(controller):
        if ALLOWED_DEVICES and _device_id(dev) not in ALLOWED_DEVICES:
            continue

        await publish_device_state(mqtt, dev, force_publish=True) # Erzwingt Publish beim Start
        await _publish_set_trigger(mqtt, dev)

    LOGGER.info("Initial state publish and set topic triggering complete.")


async def health_and_republish_loop(
    mqtt: MQTTClient,
    controller: GoveeController,
) -> None:
    """
    Periodischer State-Republish + Bridge-Health-Status.
    """
    # Setzt den Task-Namen für Debugging-Zwecke (im publish_device_state)
    asyncio.current_task().set_name("health_and_republish_loop")

    while True:
        await asyncio.sleep(STATE_INTERVAL)
        LOGGER.debug("Starting periodic state republish...")

        try:
            # Status aller Geräte erneut publizieren
            for dev in _iter_devices(controller):
                if ALLOWED_DEVICES and _device_id(dev) not in ALLOWED_DEVICES:
                    continue
                # Erzwingt Publish: Synchronisation für den Fall, dass ein Update-Paket
                # von der App verpasst wurde
                await publish_device_state(mqtt, dev, force_publish=True)

            # Health-Status der Bridge
            await mqtt.publish(
                f"{MQTT_PREFIX}/bridge/status",
                "online",
                qos=0,
                retain=False,
            )

        except MqttError as e:
            LOGGER.error("MQTT error in health/republish loop: %s", e)
        except Exception as e:
            LOGGER.exception("Unexpected error in health/republish loop: %s", e)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


async def main() -> None:
    global MQTT_PUBLISH_CLIENT

    LOGGER.info("Starting Govee MQTT bridge...")

    loop = asyncio.get_running_loop()
    controller = GoveeController(
        loop=loop,
        # Hört auf allen Interfaces
        listening_address="0.0.0.0",
        discovery_enabled=True,
        discovered_callback=discovered_callback,
        evicted_callback=lambda device: LOGGER.info("Evicted %s", device),
    )

    await controller.start()

    # Warten bis mindestens ein Device gefunden wurde
    while not controller.devices:
        LOGGER.info("Waiting for devices...")
        await asyncio.sleep(1)

    LOGGER.info("Initial devices: %s", controller.devices)

    # Hauptloop: MQTT-Verbindung aufbauen, Tasks starten, ggf. reconnecten
    while True:
        cmd_task = None
        health_task = None

        try:
            kwargs = {"client_id": "govee-bridge-state"}
            if MQTT_USERNAME:
                kwargs["username"] = MQTT_USERNAME
            if MQTT_PASSWORD:
                kwargs["password"] = MQTT_PASSWORD

            LOGGER.info("Connecting to MQTT for state publishing/republish loop...")
            async with MQTTClient(MQTT_HOST, MQTT_PORT, **kwargs) as mqtt:
                LOGGER.info("Connected to MQTT for state publishing.")

                MQTT_PUBLISH_CLIENT = mqtt

                # Initialer State + /set-Trigger
                await initial_state_publish(mqtt, controller)

                # Command-Loop und Health-Loop parallel laufen lassen
                cmd_task = asyncio.create_task(mqtt_command_loop(controller))
                health_task = asyncio.create_task(
                    health_and_republish_loop(mqtt, controller)
                )

                # Wir warten hier auf beide Loops; wenn einer fehlschlägt, wird alles neu gestartet
                await asyncio.gather(cmd_task, health_task)

        except MqttError as e:
            LOGGER.error("Fatal MQTT error in state/main loop: %s, reconnecting in 5s", e)

        except asyncio.CancelledError:
            LOGGER.info("Main tasks cancelled, shutting down...")
            break

        except Exception as e:
            LOGGER.exception("Unexpected error in main loop: %s, reconnecting in 5s", e)

        finally:
            MQTT_PUBLISH_CLIENT = None

            if cmd_task and not cmd_task.done():
                cmd_task.cancel()
            if health_task and not health_task.done():
                health_task.cancel()

            # 5 Sekunden warten, damit alle Tasks Zeit zum Aufräumen haben
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        LOGGER.info("KeyboardInterrupt received, exiting cleanly.")
