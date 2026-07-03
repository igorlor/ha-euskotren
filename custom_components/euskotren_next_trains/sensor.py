#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import logging
import os
import unicodedata
from datetime import datetime
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

LOCAL_TZ = ZoneInfo("Europe/Madrid")

CONF_GTFS_DIR = "gtfs_dir"
CONF_STOP_NAME = "stop_name"
CONF_DIRECTION = "direction"
CONF_GTFSRT_URL = "gtfsrt_url"
CONF_MAX_TRAINS = "max_trains"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Required(CONF_GTFS_DIR): cv.string,
        vol.Required(CONF_STOP_NAME): cv.string,
        vol.Required(CONF_DIRECTION): cv.string,
        vol.Optional(CONF_GTFSRT_URL, default=DEFAULT_GTFSRT_URL): cv.string,
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
    name = config[CONF_NAME]
    gtfs_dir = config[CONF_GTFS_DIR]
    stop_name = config[CONF_STOP_NAME]
    direction = config[CONF_DIRECTION]
    gtfsrt_url = config[CONF_GTFSRT_URL]
    max_trains = config[CONF_MAX_TRAINS]

    static_gtfs = await hass.async_add_executor_job(load_static_gtfs, gtfs_dir)

    stop_matches = find_stop_ids_by_name(static_gtfs, stop_name)

    if not stop_matches:
        _LOGGER.error("No se ha encontrado ninguna parada con nombre: %s", stop_name)
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