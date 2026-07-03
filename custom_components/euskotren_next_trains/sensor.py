#!/usr/bin/env python3
# -*- coding: utf-8 -*-


import io
import json
import shutil
import tempfile
import urllib.request
import zipfile
import csv
import logging
import os
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import async_timeout
import voluptuous as vol

from google.transit import gtfs_realtime_pb2

from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.const import CONF_NAME, CONF_SCAN_INTERVAL
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Euskotren Next Trains"


DEFAULT_GTFSRT_URL = (
    "https://opendata.euskadi.eus/transport/moveuskadi/"
    "euskotren/gtfsrt_euskotren_trip_updates.pb"
)

DEFAULT_GTFS_STATIC_URL = (
    "ftp://ftp.geo.euskadi.net/cartografia/Transporte/"
    "Moveuskadi/Euskotren/google_transit.zip"
)

DEFAULT_GTFS_REFRESH_HOURS = 24

CONF_GTFS_DIR = "gtfs_dir"
CONF_STOP_NAME = "stop_name"
CONF_DIRECTION = "direction"
CONF_GTFSRT_URL = "gtfsrt_url"
CONF_GTFS_STATIC_URL = "gtfs_static_url"
CONF_GTFS_REFRESH_HOURS = "gtfs_refresh_hours"
CONF_MAX_TRAINS = "max_trains"

REQUIRED_GTFS_FILES = [
    "routes.txt",
    "trips.txt",
    "stops.txt",
    "stop_times.txt",
]

LOCAL_TZ = ZoneInfo("Europe/Madrid")

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_GTFS_DIR): cv.string,
        vol.Required(CONF_STOP_NAME): cv.string,
        vol.Required(CONF_DIRECTION): cv.string,
        vol.Optional(CONF_GTFSRT_URL, default=DEFAULT_GTFSRT_URL): cv.string,
        vol.Optional(CONF_GTFS_STATIC_URL, default=DEFAULT_GTFS_STATIC_URL): cv.string,
        vol.Optional(
            CONF_GTFS_REFRESH_HOURS,
            default=DEFAULT_GTFS_REFRESH_HOURS
        ): cv.positive_int,
        vol.Optional(CONF_MAX_TRAINS, default=5): cv.positive_int,
        vol.Optional(CONF_SCAN_INTERVAL): cv.time_period,
    }
)


def normalize_text(text):
    if text is None:
        return ""

    text = str(text).strip().lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text


def read_gtfs_file(gtfs_dir, filename):
    path = os.path.join(gtfs_dir, filename)

    if not os.path.exists(path):
        raise FileNotFoundError(f"No existe el fichero GTFS requerido: {path}")

    rows = []

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    return rows


def build_lookup(rows, key_field):
    lookup = {}

    for row in rows:
        key = row.get(key_field, "")
        if key:
            lookup[key] = row

    return lookup


def build_stop_times_lookup(stop_times_rows):
    lookup = {}

    for row in stop_times_rows:
        trip_id = row.get("trip_id", "")
        stop_sequence = row.get("stop_sequence", "")

        if trip_id and stop_sequence:
            lookup[(trip_id, stop_sequence)] = row

    return lookup

def gtfs_files_exist(gtfs_dir):
    """
    Comprueba si existen los ficheros GTFS mínimos necesarios.
    """
    for filename in REQUIRED_GTFS_FILES:
        path = os.path.join(gtfs_dir, filename)
        if not os.path.exists(path):
            return False
    return True


def get_gtfs_metadata_path(gtfs_dir):
    return os.path.join(gtfs_dir, ".euskotren_gtfs_metadata.json")


def read_gtfs_metadata(gtfs_dir):
    metadata_path = get_gtfs_metadata_path(gtfs_dir)

    if not os.path.exists(metadata_path):
        return {}

    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_gtfs_metadata(gtfs_dir, url):
    metadata_path = get_gtfs_metadata_path(gtfs_dir)

    metadata = {
        "url": url,
        "downloaded_at": datetime.now(LOCAL_TZ).isoformat(),
    }

    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)


def gtfs_is_expired(gtfs_dir, refresh_hours):
    """
    Devuelve True si el GTFS estático debe actualizarse.
    """
    metadata = read_gtfs_metadata(gtfs_dir)

    downloaded_at = metadata.get("downloaded_at")
    if not downloaded_at:
        return True

    try:
        downloaded_dt = datetime.fromisoformat(downloaded_at)
    except Exception:
        return True

    age = datetime.now(LOCAL_TZ) - downloaded_dt

    return age > timedelta(hours=refresh_hours)


def download_url_sync(url, timeout=90):
    """
    Descarga HTTP/HTTPS/FTP en modo síncrono.

    Se ejecutará dentro de async_add_executor_job para no bloquear
    el event loop de Home Assistant.
    """
    _LOGGER.info("Descargando GTFS estático desde %s", url)

    headers = {
        "User-Agent": "HomeAssistant EuskotrenNextTrains/0.1",
        "Accept": "application/zip,application/octet-stream,*/*",
        "Referer": "https://www.euskadi.eus/",
    }

    request = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()

    _LOGGER.info("Descargados %s bytes desde %s", len(data), url)

    return data


def validate_gtfs_zip(zip_path):
    """
    Comprueba que el ZIP contiene los ficheros GTFS mínimos.
    """
    with zipfile.ZipFile(zip_path, "r") as z:
        names = set(z.namelist())

        missing = []

        for required_file in REQUIRED_GTFS_FILES:
            if required_file not in names:
                missing.append(required_file)

        if missing:
            raise RuntimeError(
                "El ZIP GTFS no contiene los ficheros requeridos: "
                + ", ".join(missing)
            )


def extract_gtfs_zip_atomic(zip_data, target_dir):
    """
    Extrae el GTFS de forma más segura:
    - crea directorio temporal
    - valida ZIP
    - extrae
    - sustituye contenido final
    """
    parent_dir = os.path.dirname(target_dir)
    os.makedirs(parent_dir, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=parent_dir) as tmp_dir:
        zip_path = os.path.join(tmp_dir, "gtfs.zip")
        extract_dir = os.path.join(tmp_dir, "extract")

        os.makedirs(extract_dir, exist_ok=True)

        with open(zip_path, "wb") as f:
            f.write(zip_data)

        validate_gtfs_zip(zip_path)

        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(extract_dir)

        os.makedirs(target_dir, exist_ok=True)

        for filename in os.listdir(target_dir):
            path = os.path.join(target_dir, filename)
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)

        for filename in os.listdir(extract_dir):
            src = os.path.join(extract_dir, filename)
            dst = os.path.join(target_dir, filename)
            shutil.move(src, dst)


def ensure_static_gtfs(gtfs_dir, gtfs_static_url, refresh_hours):
    """
    Garantiza que existe un GTFS estático local y actualizado.
    Esta función es síncrona y debe llamarse desde executor_job.
    """
    os.makedirs(gtfs_dir, exist_ok=True)

    must_download = False

    if not gtfs_files_exist(gtfs_dir):
        _LOGGER.info(
            "No existen todos los ficheros GTFS requeridos en %s",
            gtfs_dir
        )
        must_download = True
    elif gtfs_is_expired(gtfs_dir, refresh_hours):
        _LOGGER.info(
            "GTFS estático expirado. Se actualizará. Directorio: %s",
            gtfs_dir
        )
        must_download = True
    else:
        _LOGGER.info(
            "GTFS estático local válido. Se reutiliza: %s",
            gtfs_dir
        )

    if not must_download:
        return

    zip_data = download_url_sync(gtfs_static_url)

    extract_gtfs_zip_atomic(zip_data, gtfs_dir)

    write_gtfs_metadata(gtfs_dir, gtfs_static_url)

    _LOGGER.info("GTFS estático actualizado correctamente en %s", gtfs_dir)

def load_static_gtfs(gtfs_dir):
    _LOGGER.info("Cargando GTFS estático desde %s", gtfs_dir)

    stops = read_gtfs_file(gtfs_dir, "stops.txt")
    trips = read_gtfs_file(gtfs_dir, "trips.txt")
    routes = read_gtfs_file(gtfs_dir, "routes.txt")
    stop_times = read_gtfs_file(gtfs_dir, "stop_times.txt")

    return {
        "stops": stops,
        "trips": trips,
        "routes": routes,
        "stop_times": stop_times,
        "stop_by_id": build_lookup(stops, "stop_id"),
        "trip_by_id": build_lookup(trips, "trip_id"),
        "route_by_id": build_lookup(routes, "route_id"),
        "stop_time_by_trip_sequence": build_stop_times_lookup(stop_times),
    }


def find_stop_ids_by_name(static_gtfs, stop_name_query):
    query_norm = normalize_text(stop_name_query)

    exact_matches = []
    partial_matches = []

    for stop in static_gtfs["stops"]:
        stop_name = stop.get("stop_name", "")
        stop_name_norm = normalize_text(stop_name)

        if stop_name_norm == query_norm:
            exact_matches.append(stop)
        elif query_norm in stop_name_norm:
            partial_matches.append(stop)

    return exact_matches if exact_matches else partial_matches


def epoch_to_local_dt(epoch_value):
    return datetime.fromtimestamp(int(epoch_value), LOCAL_TZ)


def format_wait(seconds):
    if seconds < 0:
        seconds = 0

    minutes = int(seconds // 60)
    remaining_seconds = int(seconds % 60)

    if minutes == 0:
        return f"{remaining_seconds} s"

    return f"{minutes} min {remaining_seconds} s"


def trip_matches_direction(trip_static, route_static, direction_query):
    direction_norm = normalize_text(direction_query)

    trip_headsign = normalize_text(trip_static.get("trip_headsign", ""))
    route_long_name = normalize_text(route_static.get("route_long_name", ""))
    route_short_name = normalize_text(route_static.get("route_short_name", ""))

    if direction_norm in trip_headsign:
        return True

    if direction_norm in route_long_name:
        return True

    if direction_norm in route_short_name:
        return True

    return False


def get_stop_id_from_stop_time_update(stu, trip_id, static_gtfs):
    if stu.stop_id:
        return stu.stop_id

    stop_sequence = ""

    if stu.HasField("stop_sequence"):
        stop_sequence = str(stu.stop_sequence)

    if not trip_id or not stop_sequence:
        return ""

    st = static_gtfs["stop_time_by_trip_sequence"].get(
        (trip_id, stop_sequence),
        {}
    )

    return st.get("stop_id", "")


def get_event_time_from_stop_time_update(stu):
    if stu.HasField("departure") and stu.departure.HasField("time"):
        return int(stu.departure.time), "departure"

    if stu.HasField("arrival") and stu.arrival.HasField("time"):
        return int(stu.arrival.time), "arrival"

    return None, ""


def get_delay_from_stop_time_update(stu):
    if stu.HasField("departure") and stu.departure.HasField("delay"):
        return int(stu.departure.delay)

    if stu.HasField("arrival") and stu.arrival.HasField("delay"):
        return int(stu.arrival.delay)

    return None


def find_next_trains(feed, static_gtfs, target_stop_ids, direction, limit):
    now = datetime.now(LOCAL_TZ)
    now_epoch = int(now.timestamp())

    results = []

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue

        trip_update = entity.trip_update
        trip_descriptor = trip_update.trip

        trip_id = trip_descriptor.trip_id
        route_id_rt = trip_descriptor.route_id

        trip_static = static_gtfs["trip_by_id"].get(trip_id, {})

        route_id = route_id_rt or trip_static.get("route_id", "")
        route_static = static_gtfs["route_by_id"].get(route_id, {})

        if direction and not trip_matches_direction(
            trip_static,
            route_static,
            direction
        ):
            continue

        for stu in trip_update.stop_time_update:
            stop_id = get_stop_id_from_stop_time_update(
                stu,
                trip_id,
                static_gtfs
            )

            if stop_id not in target_stop_ids:
                continue

            event_epoch, event_type = get_event_time_from_stop_time_update(stu)

            if event_epoch is None:
                continue

            if event_epoch < now_epoch:
                continue

            event_dt = epoch_to_local_dt(event_epoch)
            wait_seconds = event_epoch - now_epoch
            delay_seconds = get_delay_from_stop_time_update(stu)

            stop_static = static_gtfs["stop_by_id"].get(stop_id, {})

            result = {
                "event_epoch": event_epoch,
                "time": event_dt.strftime("%H:%M:%S"),
                "datetime": event_dt.isoformat(),
                "wait_seconds": wait_seconds,
                "wait_minutes": round(wait_seconds / 60, 1),
                "wait_text": format_wait(wait_seconds),
                "event_type": event_type,
                "delay_seconds": delay_seconds,
                "trip_id": trip_id,
                "route_id": route_id,
                "route_short_name": route_static.get("route_short_name", ""),
                "route_long_name": route_static.get("route_long_name", ""),
                "trip_headsign": trip_static.get("trip_headsign", ""),
                "stop_id": stop_id,
                "stop_name": stop_static.get("stop_name", ""),
                "stop_sequence": str(stu.stop_sequence)
                if stu.HasField("stop_sequence")
                else "",
                "vehicle_id": trip_update.vehicle.id
                if trip_update.HasField("vehicle")
                else "",
                "vehicle_label": trip_update.vehicle.label
                if trip_update.HasField("vehicle")
                else "",
            }

            results.append(result)

    results.sort(key=lambda x: x["event_epoch"])

    return results[:limit]



async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    name = config.get(CONF_NAME, DEFAULT_NAME)

    gtfs_dir = config.get(CONF_GTFS_DIR)
    if not gtfs_dir:
        gtfs_dir = hass.config.path("euskotren_gtfs")

    stop_name = config[CONF_STOP_NAME]
    direction = config[CONF_DIRECTION]

    gtfsrt_url = config.get(CONF_GTFSRT_URL, DEFAULT_GTFSRT_URL)

    gtfs_static_url = config.get(
        CONF_GTFS_STATIC_URL,
        DEFAULT_GTFS_STATIC_URL
    )

    gtfs_refresh_hours = config.get(
        CONF_GTFS_REFRESH_HOURS,
        DEFAULT_GTFS_REFRESH_HOURS
    )

    max_trains = config.get(CONF_MAX_TRAINS, 5)

    await hass.async_add_executor_job(
        ensure_static_gtfs,
        gtfs_dir,
        gtfs_static_url,
        gtfs_refresh_hours,
    )

    static_gtfs = await hass.async_add_executor_job(
        load_static_gtfs,
        gtfs_dir
    )

    stop_matches = find_stop_ids_by_name(static_gtfs, stop_name)

    if not stop_matches:
        _LOGGER.error(
            "No se ha encontrado ninguna parada con nombre: %s",
            stop_name
        )
        return

    session = async_get_clientsession(hass)

    async_add_entities(
        [
            EuskotrenNextTrainsSensor(
                name=name,
                session=session,
                static_gtfs=static_gtfs,
                stop_matches=stop_matches,
                stop_name=stop_name,
                direction=direction,
                gtfsrt_url=gtfsrt_url,
                max_trains=max_trains,
            )
        ],
        True,
    )

class EuskotrenNextTrainsSensor(SensorEntity):
    def __init__(
        self,
        name,
        session,
        static_gtfs,
        stop_matches,
        stop_name,
        direction,
        gtfsrt_url,
        max_trains,
    ):
        self._attr_name = name
        self._attr_unique_id = (
            f"euskotren_next_trains_"
            f"{normalize_text(stop_name).replace(' ', '_')}_"
            f"{normalize_text(direction).replace(' ', '_')}"
        )

        self._session = session
        self._static_gtfs = static_gtfs
        self._stop_matches = stop_matches
        self._stop_name = stop_name
        self._direction = direction
        self._gtfsrt_url = gtfsrt_url
        self._max_trains = max_trains

        self._state = None
        self._available = True
        self._trains = []
        self._last_update = None
        self._error = ""

    @property
    def native_value(self):
        return self._state

    @property
    def native_unit_of_measurement(self):
        return "min"

    @property
    def icon(self):
        return "mdi:train"

    @property
    def available(self):
        return self._available

    @property
    def extra_state_attributes(self):
        return {
            "stop_name": self._stop_name,
            "direction": self._direction,
            "matching_stops": [
                {
                    "stop_id": s.get("stop_id", ""),
                    "stop_name": s.get("stop_name", ""),
                }
                for s in self._stop_matches
            ],
            "trains": self._trains,
            "next_train": self._trains[0] if self._trains else None,
            "last_update": self._last_update,
            "error": self._error,
        }

    async def async_update(self):
        target_stop_ids = {
            stop.get("stop_id", "")
            for stop in self._stop_matches
            if stop.get("stop_id", "")
        }

        headers = {
            "User-Agent": (
                "Mozilla/5.0 HomeAssistant EuskotrenNextTrains/0.1"
            ),
            "Accept": "application/octet-stream,*/*",
            "Referer": "https://www.euskadi.eus/",
        }

        try:
            async with async_timeout.timeout(30):
                async with self._session.get(
                    self._gtfsrt_url,
                    headers=headers,
                    allow_redirects=True,
                ) as response:
                    response.raise_for_status()
                    data = await response.read()

            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(data)

            trains = find_next_trains(
                feed=feed,
                static_gtfs=self._static_gtfs,
                target_stop_ids=target_stop_ids,
                direction=self._direction,
                limit=self._max_trains,
            )

            self._trains = trains
            self._last_update = datetime.now(LOCAL_TZ).isoformat()
            self._error = ""
            self._available = True

            if trains:
                self._state = trains[0]["wait_minutes"]
            else:
                self._state = None

        except Exception as exc:
            _LOGGER.exception("Error actualizando Euskotren Next Trains")
            self._available = False
            self._error = str(exc)
