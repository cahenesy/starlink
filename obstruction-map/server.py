#!/usr/bin/env python3
"""Poll Starlink get_status + dish_get_obstruction_map; serve PNG/JSON/metrics.

Talks to the dish over gRPC-web (port 9201) using only the stdlib.
Writes map.png / map.json for Grafana and exposes Prometheus metrics on :9818.
"""
from __future__ import annotations

import json
import os
import struct
import threading
import time
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DISH_HOST = os.environ.get("DISH_HOST", "192.168.100.1")
DISH_PORT = int(os.environ.get("DISH_PORT", "9201"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "10"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9818"))
OUT_DIR = os.environ.get("OUT_DIR", "/out")
PNG_SCALE = int(os.environ.get("PNG_SCALE", "4"))

FRAME_NAMES = {0: "FRAME_UNKNOWN", 1: "FRAME_EARTH", 2: "FRAME_UT"}

_lock = threading.Lock()
_state = {
    "ok": False,
    "error": "not yet scraped",
    "fetched_at": 0.0,
    "scrape_seconds": 0.0,
    "rows": 0,
    "cols": 0,
    "min_elevation_deg": 0.0,
    "max_theta_deg": 0.0,
    "map_reference_frame": 0,
    "unseen": 0,
    "clear": 0,
    "obstructed": 0,
    "partial": 0,
    "png": b"",
    "json": b"{}",
}
_status = {
    "ok": False,
    "error": "not yet scraped",
    "scrape_seconds": 0.0,
    "fetched_at": 0.0,
    "api_version": 0,
    "id": "",
    "short_id": "",
    "hardware_version": "",
    "software_version": "",
    "country_code": "",
    "build_id": "",
    "uptime_s": 0,
    "disablement_code": 0,
    "eth_speed_mbps": 0,
    "signal_quality": 0.0,
    "snr_above_noise_floor": 0,
    "snr_persistently_low": 0,
    "pop_ping_latency_ms": 0.0,
    "pop_ping_drop_rate": 0.0,
    "downlink_bps": 0.0,
    "uplink_bps": 0.0,
    "boresight_azimuth_deg": 0.0,
    "boresight_elevation_deg": 0.0,
    "tilt_angle_deg": 0.0,
    "gps_valid": 0,
    "gps_sats": 0,
    "class_of_service": 0,
    "software_update_state": 0,
    "currently_obstructed": 0,
    "fraction_obstructed": 0.0,
    "obstruction_valid_s": 0.0,
    "time_obstructed": 0.0,
    "patches_valid": 0,
}


def _enc_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _dec_varint(buf: bytes, i: int) -> tuple[int, int]:
    n = 0
    shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not (b & 0x80):
            return n, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def _parse_fields(buf: bytes) -> list[tuple[int, str, object]]:
    i = 0
    fields: list[tuple[int, str, object]] = []
    while i < len(buf):
        key, i = _dec_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _dec_varint(buf, i)
            fields.append((field, "varint", val))
        elif wire == 1:
            fields.append((field, "i64", buf[i : i + 8]))
            i += 8
        elif wire == 2:
            ln, i = _dec_varint(buf, i)
            fields.append((field, "bytes", buf[i : i + ln]))
            i += ln
        elif wire == 5:
            fields.append((field, "i32", buf[i : i + 4]))
            i += 4
        else:
            raise ValueError(f"unknown wire type {wire} at field {field}")
    return fields


def _fmap(buf: bytes) -> dict[int, list[tuple[str, object]]]:
    d: dict[int, list[tuple[str, object]]] = {}
    for f, t, v in _parse_fields(buf):
        d.setdefault(f, []).append((t, v))
    return d


def _first(d: dict, field: int, typ: str | None = None):
    for t, v in d.get(field, []):
        if typ is None or t == typ:
            return v
    return None


def _f32(raw: bytes) -> float:
    return struct.unpack("<f", raw)[0]


def _as_str(v) -> str:
    if not isinstance(v, (bytes, bytearray)):
        return ""
    try:
        s = v.decode("utf-8")
    except Exception:
        return ""
    if s and all(32 <= ord(c) < 127 for c in s):
        return s
    return ""


def _f32_field(d: dict, field: int) -> float:
    v = _first(d, field, "i32")
    return _f32(v) if v else 0.0


def _var(d: dict, field: int, default: int = 0) -> int:
    v = _first(d, field, "varint")
    return int(v) if v is not None else default


def grpc_web(field_num: int) -> bytes:
    payload = _enc_varint((field_num << 3) | 2) + _enc_varint(0)
    frame = bytes([0]) + len(payload).to_bytes(4, "big") + payload
    req = urllib.request.Request(
        f"http://{DISH_HOST}:{DISH_PORT}/SpaceX.API.Device.Device/Handle",
        data=frame,
        headers={
            "Content-Type": "application/grpc-web+proto",
            "Accept": "application/grpc-web+proto",
            "X-Grpc-Web": "1",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        body = resp.read()
    i = 0
    data = b""
    while i + 5 <= len(body):
        flags = body[i]
        i += 1
        ln = int.from_bytes(body[i : i + 4], "big")
        i += 4
        chunk = body[i : i + ln]
        i += ln
        if not (flags & 0x80):
            data += chunk
    if not data:
        raise RuntimeError("empty gRPC-web response")
    return data


def fetch_map() -> dict:
    resp = _parse_fields(grpc_web(2008))
    om_bytes = None
    for f, t, v in resp:
        if f == 2008 and t == "bytes":
            om_bytes = v
    if om_bytes is None:
        raise RuntimeError("no dish_get_obstruction_map in response")
    rows = cols = frame = 0
    min_el = max_th = 0.0
    snr_raw = b""
    for f, t, v in _parse_fields(om_bytes):
        if f == 1 and t == "varint":
            rows = int(v)
        elif f == 2 and t == "varint":
            cols = int(v)
        elif f == 3 and t == "bytes":
            snr_raw = v
        elif f == 4 and t == "i32":
            min_el = _f32(v)
        elif f == 5 and t == "i32":
            max_th = _f32(v)
        elif f == 6 and t == "varint":
            frame = int(v)
    n = rows * cols
    if rows <= 0 or cols <= 0 or len(snr_raw) != n * 4:
        raise RuntimeError(
            f"bad map shape rows={rows} cols={cols} snr_bytes={len(snr_raw)}"
        )
    snr = list(struct.unpack("<" + "f" * n, snr_raw))
    return {
        "rows": rows,
        "cols": cols,
        "min_elevation_deg": min_el,
        "max_theta_deg": max_th,
        "map_reference_frame": frame,
        "snr": snr,
    }


def fetch_status() -> dict:
    resp = _fmap(grpc_web(1004))
    api = int(_first(resp, 3, "varint") or 0)
    dish_raw = _first(resp, 2004, "bytes")
    if not dish_raw:
        raise RuntimeError("no dish_get_status in response")
    dish = _fmap(dish_raw)
    info = _fmap(_first(dish, 1, "bytes") or b"")
    state = _fmap(_first(dish, 2, "bytes") or b"")
    obs = _fmap(_first(dish, 1004, "bytes") or b"")
    gps = _fmap(_first(dish, 1015, "bytes") or b"")
    align = _fmap(_first(dish, 1027, "bytes") or b"")
    dish_id = _as_str(_first(info, 1, "bytes"))
    short = dish_id.rsplit("-", 1)[-1] if dish_id else ""
    currently = _var(obs, 5, 0)
    return {
        "api_version": api,
        "id": dish_id,
        "short_id": short,
        "hardware_version": _as_str(_first(info, 2, "bytes")),
        "software_version": _as_str(_first(info, 3, "bytes")),
        "country_code": _as_str(_first(info, 4, "bytes")),
        "build_id": _as_str(_first(info, 15, "bytes")),
        "uptime_s": _var(state, 1, 0),
        "disablement_code": _var(dish, 1024, 0),
        "eth_speed_mbps": _var(dish, 1016, 0),
        "signal_quality": _f32_field(dish, 1057),
        "snr_above_noise_floor": _var(dish, 1018, 0),
        "snr_persistently_low": _var(dish, 1022, 0),
        "pop_ping_latency_ms": _f32_field(dish, 1009),
        "pop_ping_drop_rate": _f32_field(dish, 1003),
        "downlink_bps": _f32_field(dish, 1007),
        "uplink_bps": _f32_field(dish, 1008),
        "boresight_azimuth_deg": _f32_field(align, 4) or _f32_field(dish, 1011),
        "boresight_elevation_deg": _f32_field(align, 5) or _f32_field(dish, 1012),
        "tilt_angle_deg": _f32_field(align, 3),
        "gps_valid": _var(gps, 1, 0),
        "gps_sats": _var(gps, 2, 0),
        "class_of_service": _var(dish, 1020, 0),
        "software_update_state": _var(dish, 1021, 0),
        "currently_obstructed": 1 if currently else 0,
        "fraction_obstructed": _f32_field(obs, 1),
        "obstruction_valid_s": _f32_field(obs, 4),
        "time_obstructed": _f32_field(obs, 9),
        "patches_valid": _var(obs, 10, 0),
    }


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def encode_png(snr: list[float], rows: int, cols: int, scale: int) -> bytes:
    w, h = cols * scale, rows * scale
    raw = bytearray()
    for r in range(rows):
        line = bytearray()
        base = r * cols
        for c in range(cols):
            v = snr[base + c]
            if v < 0.0:
                rgb = (0, 0, 0)
            else:
                if v > 1.0:
                    v = 1.0
                g = int(round(v * 255))
                rgb = (255, g, g)
            line.extend(rgb * scale)
        filt = b"\x00" + bytes(line)
        for _ in range(scale):
            raw.extend(filt)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _png_chunk(b"IEND", b"")
    )


def _atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def scrape_map() -> None:
    t0 = time.monotonic()
    try:
        m = fetch_map()
        snr = m["snr"]
        unseen = clear = obstructed = partial = 0
        grid: list[list[float | None]] = []
        cols = m["cols"]
        for r in range(m["rows"]):
            row: list[float | None] = []
            base = r * cols
            for c in range(cols):
                v = snr[base + c]
                if v < 0.0:
                    unseen += 1
                    row.append(None)
                else:
                    if v > 1.0:
                        v = 1.0
                    if v == 0.0:
                        obstructed += 1
                    elif v >= 0.999:
                        clear += 1
                    else:
                        partial += 1
                    row.append(v)
            grid.append(row)
        png = encode_png(snr, m["rows"], m["cols"], PNG_SCALE)
        payload = {
            "rows": m["rows"],
            "cols": m["cols"],
            "min_elevation_deg": m["min_elevation_deg"],
            "max_theta_deg": m["max_theta_deg"],
            "map_reference_frame": m["map_reference_frame"],
            "map_reference_frame_name": FRAME_NAMES.get(
                m["map_reference_frame"], str(m["map_reference_frame"])
            ),
            "unseen": unseen,
            "clear": clear,
            "obstructed": obstructed,
            "partial": partial,
            "fetched_at": time.time(),
            "snr": grid,
        }
        raw_json = json.dumps(payload, separators=(",", ":")).encode()
        _atomic_write(os.path.join(OUT_DIR, "map.png"), png)
        _atomic_write(os.path.join(OUT_DIR, "map.json"), raw_json)
        with _lock:
            _state.update(
                {
                    "ok": True,
                    "error": "",
                    "fetched_at": payload["fetched_at"],
                    "scrape_seconds": time.monotonic() - t0,
                    "rows": m["rows"],
                    "cols": m["cols"],
                    "min_elevation_deg": m["min_elevation_deg"],
                    "max_theta_deg": m["max_theta_deg"],
                    "map_reference_frame": m["map_reference_frame"],
                    "unseen": unseen,
                    "clear": clear,
                    "obstructed": obstructed,
                    "partial": partial,
                    "png": png,
                    "json": raw_json,
                }
            )
    except Exception as e:
        with _lock:
            _state["ok"] = False
            _state["error"] = f"{type(e).__name__}: {e}"
            _state["scrape_seconds"] = time.monotonic() - t0


def scrape_status() -> None:
    t0 = time.monotonic()
    try:
        st = fetch_status()
        with _lock:
            _status.update(st)
            _status["ok"] = True
            _status["error"] = ""
            _status["scrape_seconds"] = time.monotonic() - t0
            _status["fetched_at"] = time.time()
    except Exception as e:
        with _lock:
            _status["ok"] = False
            _status["error"] = f"{type(e).__name__}: {e}"
            _status["scrape_seconds"] = time.monotonic() - t0


def poller() -> None:
    while True:
        scrape_status()
        scrape_map()
        time.sleep(POLL_SECONDS)


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _g(name: str, help_: str, value, labels: dict | None = None) -> list[str]:
    lines = [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]
    if isinstance(value, float) and (value != value):  # NaN
        rendered = "NaN"
    else:
        rendered = str(value)
    if labels:
        lab = ",".join(f'{k}="{_esc(str(v))}"' for k, v in labels.items())
        lines.append(f"{name}{{{lab}}} {rendered}")
    else:
        lines.append(f"{name} {rendered}")
    return lines


def metrics_text() -> bytes:
    with _lock:
        s = dict(_state)
        st = dict(_status)
    frame_name = FRAME_NAMES.get(s["map_reference_frame"], "unknown")
    lines: list[str] = []
    lines += _g(
        "starlink_status_up",
        "1 if the last get_status scrape succeeded",
        1 if st["ok"] else 0,
    )
    lines += _g(
        "starlink_status_scrape_duration_seconds",
        "Last get_status scrape duration",
        st["scrape_seconds"],
    )
    lines += _g(
        "starlink_status_info",
        "Dish identity labels from DeviceInfo (value always 1)",
        1 if st["ok"] else 0,
        {
            "id": st["id"],
            "short_id": st["short_id"],
            "hardware_version": st["hardware_version"],
            "software_version": st["software_version"],
            "country_code": st["country_code"],
            "build_id": st["build_id"],
        },
    )
    lines += _g("starlink_status_uptime_seconds", "Dish uptime from DeviceState.uptime_s", st["uptime_s"])
    lines += _g(
        "starlink_status_disablement_code",
        "UtDisablementCode (1=OKAY)",
        st["disablement_code"],
    )
    lines += _g("starlink_status_eth_speed_mbps", "Ethernet speed reported by the dish", st["eth_speed_mbps"])
    lines += _g("starlink_status_signal_quality", "Dish signal_quality in 0..1", st["signal_quality"])
    lines += _g(
        "starlink_status_snr_above_noise_floor",
        "1 if dish reports SNR above noise floor",
        st["snr_above_noise_floor"],
    )
    lines += _g(
        "starlink_status_snr_persistently_low",
        "1 if dish reports persistently low SNR",
        st["snr_persistently_low"],
    )
    lines += _g("starlink_status_pop_ping_latency_ms", "PoP ping latency in milliseconds", st["pop_ping_latency_ms"])
    lines += _g("starlink_status_pop_ping_drop_ratio", "PoP ping drop rate", st["pop_ping_drop_rate"])
    lines += _g("starlink_status_downlink_bps", "Downlink throughput bits per second", st["downlink_bps"])
    lines += _g("starlink_status_uplink_bps", "Uplink throughput bits per second", st["uplink_bps"])
    lines += _g("starlink_status_boresight_azimuth_deg", "Boresight azimuth degrees", st["boresight_azimuth_deg"])
    lines += _g("starlink_status_boresight_elevation_deg", "Boresight elevation degrees", st["boresight_elevation_deg"])
    lines += _g("starlink_status_tilt_angle_deg", "Dish tilt angle degrees", st["tilt_angle_deg"])
    lines += _g("starlink_status_gps_valid", "1 if dish GPS is valid", st["gps_valid"])
    lines += _g("starlink_status_gps_sats", "GPS satellites used by the dish", st["gps_sats"])
    lines += _g("starlink_status_class_of_service", "UserClassOfService enum", st["class_of_service"])
    lines += _g("starlink_status_software_update_state", "SoftwareUpdateState enum", st["software_update_state"])
    lines += _g("starlink_status_currently_obstructed", "1 if currently obstructed", st["currently_obstructed"])
    lines += _g("starlink_status_fraction_obstructed", "Fraction of sky considered obstructed", st["fraction_obstructed"])
    lines += _g("starlink_status_obstruction_valid_seconds", "Seconds of valid obstruction statistics", st["obstruction_valid_s"])
    lines += _g("starlink_status_time_obstructed_ratio", "time_obstructed fraction from DishObstructionStats", st["time_obstructed"])
    lines += _g("starlink_status_patches_valid", "Obstruction map patches_valid", st["patches_valid"])
    lines += _g("starlink_status_api_version", "Dish Handle api_version", st["api_version"])
    lines.append(f"# status_error {st['error']}" if st["error"] else "# status_error none")

    lines += _g("starlink_obstruction_map_up", "1 if the last dish_get_obstruction_map scrape succeeded", 1 if s["ok"] else 0)
    lines += _g("starlink_obstruction_map_rows", "Obstruction map row count", s["rows"])
    lines += _g("starlink_obstruction_map_cols", "Obstruction map column count", s["cols"])
    lines += _g("starlink_obstruction_map_unseen_cells", "Cells never observed (snr < 0)", s["unseen"])
    lines += _g("starlink_obstruction_map_clear_cells", "Cells with snr ~= 1", s["clear"])
    lines += _g("starlink_obstruction_map_obstructed_cells", "Cells with snr == 0", s["obstructed"])
    lines += _g("starlink_obstruction_map_partial_cells", "Cells with 0 < snr < 1", s["partial"])
    lines += _g("starlink_obstruction_map_min_elevation_deg", "Minimum elevation of the map", s["min_elevation_deg"])
    lines += _g("starlink_obstruction_map_max_theta_deg", "Maximum theta of the map", s["max_theta_deg"])
    lines += _g("starlink_obstruction_map_scrape_duration_seconds", "Last map scrape duration", s["scrape_seconds"])
    lines += _g("starlink_obstruction_map_fetched_unix_seconds", "Unix time of last successful map", s["fetched_at"])
    lines.append(f"# map_reference_frame {s['map_reference_frame']} ({frame_name})")
    lines.append(f"# map_error {s['error']}" if s["error"] else "# map_error none")
    lines.append("")
    return ("\n".join(lines)).encode()


HTML = """<!doctype html>
<meta charset="utf-8">
<title>Starlink obstruction map</title>
<style>
  body { margin:0; background:#111; color:#ddd; font:14px/1.4 sans-serif; }
  .wrap { padding:12px; }
  img { image-rendering: pixelated; width: min(92vw, 640px); height: auto; background:#000; }
  .meta { margin: 8px 0 12px; color:#aaa; }
</style>
<div class="wrap">
  <div class="meta" id="meta">loading…</div>
  <div>N</div>
  <img id="map" alt="obstruction map" src="/map.png">
  <div style="display:flex;width:min(92vw,640px);justify-content:space-between">
    <span>W</span><span>E</span>
  </div>
  <div>S · red = obstructed · white = clear · black = unseen</div>
</div>
<script>
async function tick() {
  try {
    const m = await fetch('/map.json', {cache:'no-store'}).then(r => r.json());
    document.getElementById('meta').textContent =
      m.rows + '×' + m.cols + '  ' + (m.map_reference_frame_name||'') +
      '  unseen ' + m.unseen + '  obstructed ' + m.obstructed +
      '  partial ' + m.partial + '  clear ' + m.clear;
    document.getElementById('map').src = '/map.png?t=' + Date.now();
  } catch (e) {
    document.getElementById('meta').textContent = String(e);
  }
}
tick();
setInterval(tick, 10000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, code: int, ctype: str, body: bytes, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", HTML.encode())
            return
        if path == "/metrics":
            self._send(200, "text/plain; version=0.0.4; charset=utf-8", metrics_text())
            return
        if path in ("/health", "/healthz"):
            with _lock:
                ok = _state["ok"] and _status["ok"]
                err = _status["error"] or _state["error"]
            body = (b"ok\n" if ok else f"fail: {err}\n".encode())
            self._send(200 if ok else 503, "text/plain; charset=utf-8", body)
            return
        if path == "/map.png":
            with _lock:
                png = _state["png"]
            if not png:
                self._send(503, "text/plain; charset=utf-8", b"no map yet\n")
                return
            self._send(200, "image/png", png)
            return
        if path == "/map.json":
            with _lock:
                raw = _state["json"]
                ok = _state["ok"] or bool(_state["fetched_at"])
            if not ok:
                self._send(503, "application/json", json.dumps({"error": _state["error"]}).encode())
                return
            self._send(200, "application/json", raw)
            return
        self._send(404, "text/plain; charset=utf-8", b"not found\n")


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    threading.Thread(target=poller, name="poller", daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(
        f"obstruction-map listening :{LISTEN_PORT} dish={DISH_HOST}:{DISH_PORT} out={OUT_DIR}",
        flush=True,
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
