#!/usr/bin/env python3
"""
BPQ "DIRECTIONS" application (RF-first)

The node command is DIRECTIONS (BPQ already has ROUTES built in, and ROUTE
read as that); inside the app a trip is still asked for with ROUTE.

Offline driving directions, place lookup and "what is near" for BPQ32 packet
nodes, in the style of WX/RPTSRCH:
- BPQ connects via Telnet port CMDPORT (HOST n -> 127.0.0.1:63053)
- BPQ sends the user's callsign as the first line (APPLICATION ... S flag)
- Per-callsign HOME remembered in route_users.json
- Directions come from a GraphHopper server and a gazetteer (place / street /
  POI / zip lookup) that OffgridAI hosts on the LAN; nothing here needs the
  internet. Both are HTTP JSON; see README.md for running them yourself.
- Output is built for a 42-column packet terminal: a route is a table of
  numbered turns, about 40 bytes a row, paged with MORE / BACK / TOP / ALL.
- YAPP sends the last route to the user's PC as a .txt (sender ported from WX).

Commands:
  (Enter)              -> your HOME card (grid, lat/lon, map date)
  ROUTE <a> TO <b>     -> driving directions a -> b
  ROUTE <b>            -> from HOME to b
  WHERE <place>        -> grid, lat/lon, distance + bearing from HOME
  NEAR [<place>]       -> nearest towns / landmarks to place (or HOME)
  HOME <zip|grid|place>-> set HOME for this callsign
  MORE / BACK          -> next / previous page of the current route
  TOP / ALL            -> first page / the rest of the route at once
  YAPP                 -> send the current route to your PC by YAPP
  HELP                 -> show commands
  Q / BYE / EXIT / QUIT / NODE -> exit

A <place> is a zip (33445), a Maidenhead grid (EL96xl), "lat,lon", a town
("Boca Raton"), a street with its town ("Congress Ave, Delray Beach") or a
named landmark ("Bethesda Hospital"). When a name matches more than one thing
the app prints a numbered pick list and takes a bare number in reply.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import re
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# -----------------------------
# Config
# -----------------------------

DEFAULT_CONFIG = {
    "bpq_app": {
        "listen_host": "127.0.0.1",
        "listen_port": 63053
    },
    "engine": {
        "_comment": "The OffgridAI box (or any host) running GraphHopper and the gazetteer. See README.",
        "graphhopper_url": "http://192.168.1.50:8989",
        "gazetteer_url": "http://192.168.1.50:8991",
        "timeout_s": 20
    },
    "storage": {
        "user_db_file": "route_users.json"
    },
    "output_dir": "C:\\Users\\Jason\\AppData\\Roaming\\BPQ32\\BPQMailChat\\Files",
    "banner": "SFLDIGI DIRECTIONS SERVICE - N4SFL NODE",
    "file_max_age_hours": 24,
    "display": {
        "_comment": "Fold output at this many columns (mobile packet clients are ~43); 0 disables. menu_every_reply shows the menu after every reply. steps_per_page = route rows per screen before MORE.",
        "width": 42,
        "menu_every_reply": False,
        "steps_per_page": 14
    }
}
CONFIG_FILE = "route_config.json"

MENU_LINE = ("<ENTER> | ROUTE <from> TO <to>\r\n"
             "ROUTE <to> | WHERE <place> | NEAR <place>\r\n"
             "HOME <zip> | MORE | YAPP | HELP | QUIT\r\n")

# Two columns inside 42: a 16-wide command field, then the gloss.
HELP_TEXT = (
    "Commands:\r\n"
    "  (Enter)         your HOME card\r\n"
    "  ROUTE <a> TO <b>  driving directions\r\n"
    "  ROUTE <b>       from HOME to <b>\r\n"
    "  WHERE <place>   grid, lat/lon, bearing\r\n"
    "  NEAR <place>    towns/landmarks near it\r\n"
    "  NEAR            same, around HOME\r\n"
    "  HOME <place>    set HOME (zip, grid or\r\n"
    "                  place name)\r\n"
    "  MORE / BACK     next / previous page\r\n"
    "  TOP / ALL       first page / the rest\r\n"
    "  YAPP            send route to your PC\r\n"
    "  HELP            show commands\r\n"
    "  Q / BYE / NODE  exit\r\n"
    "A place: zip 33445, grid EL96xl,\r\n"
    "lat,lon, a town, 'Congress Ave, Delray\r\n"
    "Beach', or a landmark like 'Bethesda\r\n"
    "Hospital'. If it matches more than one\r\n"
    "thing you get a numbered list: reply\r\n"
    "with the number.\r\n"
    "Turns: L R left/right, SL SR slight,\r\n"
    "HL HR hard, KL KR keep, RB2 roundabout\r\n"
    "exit 2, U u-turn, GO continue, END.\r\n"
)

# House vocabulary, identical in all the node apps.
EXIT_WORDS = ("Q", "QUIT", "EXIT", "BYE", "NODE")
HELP_WORDS = ("HELP", "?")
MORE_WORDS = ("MORE", "M", "NEXT")
# Route paging beyond MORE; not (yet) house vocabulary, so only this app has them.
BACK_WORDS = ("BACK", "B", "PREV")
TOP_WORDS = ("TOP", "FIRST")
ALL_WORDS = ("ALL",)
ROUTE_VERBS = ("ROUTE", "RT", "DIRECTIONS", "DIR")

MI = 1609.344


# -----------------------------
# Output sanitization (BBS-safe, ASCII + CR)
# -----------------------------

def sanitize_for_bbs(s: str) -> str:
    """Normalize to plain ASCII with CR line endings (BPQ native convention).
    The app runs on a TRANS (binary) HOST connection, so the node passes our
    bytes through unmodified - we must emit CR, not CRLF, ourselves."""
    if not s:
        return s
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = (s.replace("\u201c", '"').replace("\u201d", '"')
           .replace("\u2018", "'").replace("\u2019", "'")
           .replace("\u2014", "-").replace("\u2013", "-")
           .replace("\u2026", "..."))
    s = re.sub(r"[^\x09\x0a\x20-\x7e]", "", s)
    return s.replace("\n", "\r")


OUT_WIDTH = 42
MENU_EVERY = False
STEPS_PER_PAGE = 14


def wrap_out(text: str, width: int) -> str:
    """Fold over-long lines at word boundaries for a narrow terminal.

    Lines already inside the width pass through untouched, which is what keeps
    the route table in column. See BPQ-Node/NODE-APP-STYLE.md.
    """
    if width <= 0 or not text:
        return text
    out = []
    for raw in text.replace("\r\n", "\n").splitlines(keepends=True):
        end = "\n" if raw.endswith("\n") else ""
        body = raw[:-1] if end else raw
        if len(body) <= width:
            out.append(body + end)
            continue
        lead = body[:len(body) - len(body.lstrip(" "))]
        folded = textwrap.wrap(body.strip(), width=width,
                               initial_indent=lead,
                               subsequent_indent=lead or "  ",
                               break_long_words=True, break_on_hyphens=False)
        out.append("\n".join(folded or [body]) + end)
    return "".join(out)


def apply_display_config(cfg: Dict) -> None:
    """Adopt display.* from config. A typo must not stop the node coming up."""
    global OUT_WIDTH, MENU_EVERY, STEPS_PER_PAGE
    d = (cfg.get("display") or {}) if isinstance(cfg, dict) else {}
    try:
        OUT_WIDTH = int(d.get("width", OUT_WIDTH))
    except (TypeError, ValueError):
        print(f"[CFG] display.width unreadable - keeping {OUT_WIDTH}.")
    try:
        STEPS_PER_PAGE = max(3, int(d.get("steps_per_page", STEPS_PER_PAGE)))
    except (TypeError, ValueError):
        print(f"[CFG] display.steps_per_page unreadable - keeping {STEPS_PER_PAGE}.")
    MENU_EVERY = bool(d.get("menu_every_reply", MENU_EVERY))


def send(writer: asyncio.StreamWriter, text: str) -> None:
    # Fold first: sanitize_for_bbs turns newlines into the bare CR a TRANS
    # connection wants, which is no longer a line break to fold on.
    writer.write(sanitize_for_bbs(wrap_out(text, OUT_WIDTH)).encode("utf-8", "ignore"))


# -----------------------------
# Maidenhead / geometry (same code as OffgridAI routing/gazetteer/maidenhead.py)
# -----------------------------

GRID_RE = re.compile(r"^[A-Ra-r]{2}[0-9]{2}(?:[A-Xa-x]{2}(?:[0-9]{2})?)?$")


def is_grid(s: str) -> bool:
    return bool(GRID_RE.match((s or "").strip()))


def to_grid(lat: float, lon: float, precision: int = 6) -> str:
    lon = (lon + 180.0) % 360.0
    lat = lat + 90.0
    A = ord("A")
    out = chr(A + int(lon // 20)) + chr(A + int(lat // 10))
    lon, lat = lon % 20, lat % 10
    out += str(int(lon // 2)) + str(int(lat // 1))
    if precision >= 6:
        lon, lat = lon % 2, lat % 1
        out += chr(A + int(lon / (2 / 24))).lower() + chr(A + int(lat / (1 / 24))).lower()
    return out


def haversine_mi(lat1, lon1, lat2, lon2) -> float:
    R = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def compass(deg: float) -> str:
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return pts[int((deg + 11.25) // 22.5) % 16]


# -----------------------------
# Per-user state
# -----------------------------

class UserDB:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data: Dict[str, Dict] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def save(self):
        # Read-modify-write so two instances cannot wipe each other's users.
        self._load_merge()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _load_merge(self):
        if self.path.exists():
            try:
                on_disk = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return
            for k, v in on_disk.items():
                self.data.setdefault(k, v)

    def get_home(self, callsign: str) -> Optional[Dict]:
        cs = base_call(callsign)
        return (self.data.get(cs, {}) or {}).get("home")

    def set_home(self, callsign: str, home: Dict) -> None:
        cs = base_call(callsign)
        self.data.setdefault(cs, {})["home"] = home
        self.save()


def base_call(callsign: str) -> str:
    cs = (callsign or "UNKNOWN").upper().split("-")[0]
    return re.sub(r"[^A-Z0-9]", "", cs) or "UNKNOWN"


# -----------------------------
# Engine clients (gazetteer + GraphHopper), plain urllib
# -----------------------------

class EngineError(Exception):
    pass


class Engine:
    def __init__(self, cfg: Dict):
        e = cfg["engine"]
        self.gh = e["graphhopper_url"].rstrip("/")
        self.gz = e["gazetteer_url"].rstrip("/")
        self.timeout = float(e.get("timeout_s", 20))
        self._meta: Optional[Dict] = None
        self._meta_at = 0.0

    def _get(self, url: str) -> Dict:
        req = urllib.request.Request(url, headers={"User-Agent": "bpq-route/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
                msg = body.get("message") or body.get("error") or str(e)
            except Exception:
                msg = str(e)
            raise EngineError(msg[:120])
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise EngineError(f"engine unreachable ({str(e)[:60]})")

    def geocode(self, q: str, near: Optional[Tuple[float, float]] = None, limit: int = 6) -> List[Dict]:
        params = {"q": q, "limit": limit}
        if near:
            params["near"] = f"{near[0]:.5f},{near[1]:.5f}"
        return self._get(f"{self.gz}/geocode?{urllib.parse.urlencode(params)}").get("results", [])

    def near(self, lat: float, lon: float, limit: int = 12, radius_mi: float = 25.0) -> List[Dict]:
        params = {"lat": f"{lat:.5f}", "lon": f"{lon:.5f}", "limit": limit,
                  "radius_mi": radius_mi, "kinds": "place,poi"}
        return self._get(f"{self.gz}/near?{urllib.parse.urlencode(params)}").get("results", [])

    def route(self, a: Dict, b: Dict) -> Dict:
        params = [("point", f"{a['lat']:.5f},{a['lon']:.5f}"), ("point", f"{b['lat']:.5f},{b['lon']:.5f}"),
                  ("profile", "car"), ("locale", "en"), ("instructions", "true"),
                  ("calc_points", "true"), ("points_encoded", "true"),
                  ("details", "road_class"), ("details", "road_class_link")]
        d = self._get(f"{self.gh}/route?{urllib.parse.urlencode(params)}")
        paths = d.get("paths") or []
        if not paths:
            raise EngineError("no route found")
        return paths[0]

    def meta(self) -> Dict:
        """Gazetteer build metadata (region, map date); cached 10 minutes."""
        if self._meta and time.monotonic() - self._meta_at < 600:
            return self._meta
        try:
            self._meta = self._get(f"{self.gz}/health")
        except EngineError:
            self._meta = self._meta or {}
        self._meta_at = time.monotonic()
        return self._meta

    def map_line(self) -> str:
        m = self.meta()
        # "florida-latest.osm.pbf" / "us-20260924.osm.pbf" -> "Florida" / "USA"
        region = re.sub(r"(-latest|-\d{8})?\.osm\.pbf$", "", m.get("region_file") or "region")
        region = {"us": "USA", "us-south": "US South", "us-northeast": "US Northeast",
                  "us-midwest": "US Midwest", "us-west": "US West"}.get(region, region.replace("-", " ").title())
        date = m.get("data_date", "?")
        return f"Map: OSM {region} {date}"


# -----------------------------
# Formatting
# -----------------------------

ABBREV = [("Avenue", "Ave"), ("Street", "St"), ("Road", "Rd"), ("Boulevard", "Blvd"),
          ("Drive", "Dr"), ("Lane", "Ln"), ("Court", "Ct"), ("Place", "Pl"),
          ("Highway", "Hwy"), ("Parkway", "Pkwy"), ("Terrace", "Ter"), ("Circle", "Cir"),
          ("Trail", "Trl"), ("Expressway", "Expy"), ("Turnpike", "Tpke"), ("Causeway", "Cswy"),
          ("Northeast", "NE"), ("Northwest", "NW"), ("Southeast", "SE"), ("Southwest", "SW"),
          ("North", "N"), ("South", "S"), ("East", "E"), ("West", "W"),
          ("Fort", "Ft"), ("Mount", "Mt"), ("Point", "Pt")]
_ABBR = {a.lower(): b for a, b in ABBREV}
# 4 = finish, 5 = via point, 6 = roundabout (exit number separate)
TURN = {-3: "HL", -2: "L", -1: "SL", 0: "GO", 1: "SR", 2: "R", 3: "HR", 4: "END", 5: "VIA",
        7: "KR", -7: "KL", 8: "U", -8: "U", -98: "U"}
TYPE_SHORT = {
    "city": "city", "town": "town", "village": "village", "hamlet": "hamlet", "suburb": "area",
    "neighbourhood": "nbhd", "quarter": "nbhd", "locality": "place", "island": "island", "islet": "islet",
    "hospital": "hosp", "clinic": "clinic", "doctors": "clinic", "pharmacy": "pharm",
    "fire station": "fire", "police": "police", "school": "school", "university": "college",
    "college": "college", "library": "library", "townhall": "cityhal", "courthouse": "court",
    "community centre": "commctr", "place of worship": "church", "fuel": "fuel",
    "ferry terminal": "ferry", "bus station": "bus", "shelter": "shelter", "post office": "post",
    "ranger station": "ranger", "marketplace": "market", "ambulance station": "ems",
    "assembly point": "assembly", "aerodrome": "airport", "heliport": "helipad", "station": "rail",
    "marina": "marina", "park": "park", "nature reserve": "reserve", "golf course": "golf",
    "stadium": "stadium", "national park": "natpark", "protected area": "reserve",
    "camp site": "camp", "attraction": "sight", "museum": "museum", "viewpoint": "view",
    "lighthouse": "lthouse", "tower": "tower", "mast": "tower", "communications tower": "tower",
    "pier": "pier", "water tower": "wtower", "beach": "beach", "peak": "peak", "spring": "spring",
    "cape": "cape", "water": "lake", "bay": "bay", "wood": "woods", "supermarket": "grocery",
    "mall": "mall", "hardware": "hardwre", "doityourself": "hardwre", "cemetery": "cemetry",
    "military": "militry",
}


def abbrev(name: str) -> str:
    name = re.split(r"[;,]", name, maxsplit=1)[0].strip()
    s = re.sub(r"[A-Za-z]+", lambda m: _ABBR.get(m.group().lower(), m.group()), name)
    s = re.sub(r"\b(I|US|SR|CR|FL) (\d+)", r"\1-\2", s)
    return s


def fit(s: str, width: int) -> str:
    s = s or ""
    return s if len(s) <= width else s[:width - 1] + "~"


def road_label(ins: Dict) -> str:
    name = ins.get("street_name") or ""
    ref = ins.get("street_ref") or ""
    if name and ref:
        lab = abbrev(name)
        both = f"{lab}/{abbrev(ref)}"
        return both if len(both) <= 25 else lab
    if name or ref:
        return abbrev(name or ref)
    dest_ref = ins.get("street_destination_ref") or ""
    dest = ins.get("street_destination") or ""
    if dest_ref:
        return "to " + abbrev(dest_ref)
    if dest:
        return "to " + abbrev(dest)
    if ins.get("sign") == 4:
        return "arrive"
    return "unnamed rd"


def road_kind(ins: Dict, details: Dict) -> str:
    """What an unnamed step is, from the road class GraphHopper returns alongside
    the instructions: 'ramp' (a motorway/trunk link, e.g. between SR-706 and the
    Turnpike), 'svc rd' (parking lots, driveways - where a grid-centre start lands),
    else 'unnamed rd'. Majority by points over the step's interval."""
    a, b = (ins.get("interval") or (0, 0))[:2]

    def majority(key: str):
        tally: Dict = {}
        for s, e, v in details.get(key) or []:
            n = min(b, e) - max(a, s)
            if n > 0:
                tally[v] = tally.get(v, 0) + n
        return max(tally, key=tally.get) if tally else None

    if majority("road_class_link"):
        return "ramp"
    if majority("road_class") == "service":
        return "svc rd"
    return "unnamed rd"


def turn_code(ins: Dict) -> str:
    sign = ins.get("sign", 0)
    if sign in (6, -6):
        return f"RB{ins.get('exit_number', '')}"
    return TURN.get(sign, "GO")


UNNAMED = ("unnamed rd", "svc rd", "ramp")


def compress_steps(instructions: List[Dict], details: Optional[Dict] = None) -> List[Tuple[str, str, float]]:
    """(turn, road, metres) per row. An unnamed step is called what it is (ramp,
    svc rd) when the road class says so. Consecutive short unnamed steps - parking
    lots, service roads, ramp splits - become one row ('svc rds', or 'ramp' if a
    ramp is among them), because five rows of '-' cost airtime and say nothing."""
    out: List[Tuple[str, str, float]] = []
    for ins in instructions:
        turn, road, dist = turn_code(ins), road_label(ins), float(ins.get("distance", 0.0))
        if road == "unnamed rd" and details:
            road = road_kind(ins, details)
        prev = out[-1][1].rstrip("s") if out else ""
        if road in UNNAMED and prev in UNNAMED and dist < 0.25 * MI:
            # A ramp is the useful word in a run; otherwise one plural row.
            pturn, _, pdist = out[-1]
            if "ramp" in (road, prev):
                merged = "ramp"
            else:
                merged = (road if road == prev else "unnamed rd") + "s"
            out[-1] = (pturn, merged, pdist + dist)
            continue
        out.append((turn, road, dist))
    return out


def fmt_mi(m: float) -> str:
    mi = m / MI
    if mi < 0.05:
        return "<.1"
    return f"{mi:.1f}" if mi < 100 else f"{mi:.0f}"


def fmt_time(ms: float) -> str:
    mins = int(round(ms / 60000.0))
    if mins < 60:
        return f"~{max(1, mins)}min"
    return f"~{mins // 60}h{mins % 60:02d}"


def short_place(item: Dict) -> str:
    """One short handle for a resolved place, for titles."""
    if item.get("kind") == "zip":
        return f"{item['name']} {item.get('city', '')} {item.get('state', '')}".strip()
    if item.get("kind") in ("grid", "point"):
        return item["name"]
    if item.get("kind") == "place":
        return f"{item['name']} {item.get('state', '')}".strip()
    city = item.get("city") or ""
    # A long landmark name stands on its own; adding the town only folds the title.
    return f"{item['name']}, {city}" if city and len(item["name"]) <= 26 else item["name"]


class RouteDoc:
    """A computed route rendered once into fixed-width rows, then paged."""

    def __init__(self, a: Dict, b: Dict, path: Dict, map_line: str):
        self.title = (f"RT {path['distance'] / MI:.1f}mi {fmt_time(path['time'])}\r\n"
                      f"{short_place(a)} > {short_place(b)}\r\n")
        self.header = " #  TURN  ROAD                       MI\r\n"
        self.rows: List[str] = []
        for i, (turn, road, dist) in enumerate(compress_steps(path.get("instructions", []), path.get("details")), 1):
            self.rows.append(f"{i:>2}  {turn:<4}  {fit(road, 25):<25}  {fmt_mi(dist):>5}\r\n")
        self.footer = map_line + " via GraphHopper\r\n"
        self.page = 0
        self.pages = max(1, math.ceil(len(self.rows) / STEPS_PER_PAGE))

    def nav_line(self) -> str:
        """What can be done from the page on screen - only what applies there,
        so a one-page route is not offered MORE it cannot use."""
        more, back = self.page + 1 < self.pages, self.page > 0
        opts = (["MORE"] if more else []) + (["BACK"] if back else []) + \
               (["ALL"] if more else ["TOP"] if back else []) + ["YAPP", "HELP", "QUIT"]
        return " | ".join(opts) + "\r\n"

    def render_page(self, page: int, upto: Optional[int] = None) -> str:
        """One page, or pages `page`..`upto` as one screen (ALL)."""
        page = max(0, min(page, self.pages - 1))
        last = page if upto is None else max(page, min(upto, self.pages - 1))
        self.page = last
        lo, hi = page * STEPS_PER_PAGE, min(len(self.rows), (last + 1) * STEPS_PER_PAGE)
        n = len(self.rows)
        out = (self.title if page == 0 else "") + self.header + "".join(self.rows[lo:hi])
        if hi < n:
            out += f"{lo + 1}-{hi} of {n} - MORE for {min(STEPS_PER_PAGE, n - hi)} more\r\n"
        else:
            out += f"{lo + 1}-{hi} of {n} - end of route\r\n" + self.footer
        return out + self.nav_line()

    def next_page(self) -> Optional[str]:
        if self.page + 1 >= self.pages:
            return None
        return self.render_page(self.page + 1)

    def prev_page(self) -> Optional[str]:
        if self.page == 0:
            return None
        return self.render_page(self.page - 1)

    def rest(self) -> Optional[str]:
        if self.page + 1 >= self.pages:
            return None
        return self.render_page(self.page + 1, upto=self.pages - 1)

    def full_text(self, banner: str) -> str:
        return (self.title + self.header + "".join(self.rows) + self.footer +
                f"\r\n{banner}\r\nGenerated on demand by the DIRECTIONS app.\r\n")


def where_text(item: Dict, home: Optional[Dict]) -> str:
    out = f"WHERE {item.get('label') or short_place(item)}\r\n"
    typ = item.get("sub") or item.get("kind") or ""
    if typ and typ not in ("grid", "point", "zip"):
        out += f"Type: {typ}\r\n"
    out += f"Grid: {item.get('grid') or to_grid(item['lat'], item['lon'])}  {item['lat']:.4f},{item['lon']:.4f}\r\n"
    if home:
        d = haversine_mi(home["lat"], home["lon"], item["lat"], item["lon"])
        b = bearing_deg(home["lat"], home["lon"], item["lat"], item["lon"])
        out += f"From HOME: {d:.1f}mi {compass(b)} ({int(round(b))} deg)\r\n"
    return out


def near_text(center: Dict, items: List[Dict], radius_mi: float) -> str:
    out = f"NEAR {radius_mi:.0f}mi: {short_place(center)}\r\n"
    if not items:
        return out + "Nothing named within range.\r\n"
    out += "NAME                   TYPE       MI  BRG\r\n"
    for it in items:
        typ = TYPE_SHORT.get(it.get("sub") or "", (it.get("sub") or it.get("kind") or "")[:7])
        out += f"{fit(it['name'], 21):<21}  {typ:<7}  {it['dist_mi']:>5.1f}  {it.get('compass', ''):<3}\r\n"
    return out


def home_card(home: Optional[Dict], map_line: str) -> str:
    if not home:
        return "No HOME set. HOME <zip>, e.g. HOME 33445\r\n" + map_line + "\r\n"
    return (f"HOME {home['label']}\r\n"
            f"Grid: {home['grid']}  {home['lat']:.4f},{home['lon']:.4f}\r\n"
            f"{map_line}\r\n")


def home_from_item(item: Dict) -> Dict:
    return {"label": short_place(item), "lat": item["lat"], "lon": item["lon"],
            "grid": item.get("grid") or to_grid(item["lat"], item["lon"])}


def pick_text(q: str, cands: List[Dict]) -> str:
    out = f"Which '{q}'?\r\n"
    for i, c in enumerate(cands, 1):
        typ = c.get("sub") or ("street" if c.get("kind") == "street" else "")
        lab = c.get("label") or short_place(c)
        out += f"{i} {lab}" + (f" ({typ})" if typ else "") + "\r\n"
    out += f"Reply with a number 1-{len(cands)}.\r\n"
    return out


# -----------------------------
# Files + YAPP (sender ported from WX; pacing comments are load-bearing)
# -----------------------------

def prune_old_files(output_dir: Path, max_age_hours: int) -> None:
    cutoff = time.time() - max_age_hours * 3600
    try:
        for p in output_dir.glob("ROUTE_*.txt"):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
    except Exception:
        pass


def write_route_file(output_dir: Path, callsign: str, doc: RouteDoc, banner: str) -> str:
    # Time in the name keeps each transfer unique - YAPP receivers (QtTermTCP)
    # refuse a file that already exists in their receive folder.
    date_s = dt.datetime.now().strftime("%m-%d-%Y_%H%M")
    name = f"ROUTE_{base_call(callsign)}_{date_s}.txt"
    dest = output_dir / name
    dest.write_text(sanitize_for_bbs(doc.full_text(banner)).replace("\r", "\n"), encoding="utf-8")
    print(f"[FILE] Wrote {dest.resolve()}")
    return name


Y_SOH, Y_STX, Y_ETX, Y_EOT = 0x01, 0x02, 0x03, 0x04
Y_ENQ, Y_ACK, Y_NAK, Y_CAN = 0x05, 0x06, 0x15, 0x18
YAPP_BLOCK = 128
# Gap between YAPP frames so each is flushed through the node as its own
# packet (keeps per-packet YAPP parsers in sync).
FRAME_GAP = 0.05


class YappAbort(Exception):
    pass


def _bbs_fallback(name: str) -> str:
    return (f"To download it through the BBS instead, type these commands:\r\n"
            f"  QUIT\r\n"
            f"  BBS\r\n"
            f"  YAPP {name}\r\n")


async def _yapp_wait(reader: asyncio.StreamReader, timeout: float) -> Tuple[str, int, bytes]:
    """Wait for a YAPP control frame, skipping stray text bytes (echoes,
    CR/LF) the terminal may emit before its YAPP engine engages."""
    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise asyncio.TimeoutError()
        b = await asyncio.wait_for(reader.readexactly(1), timeout=remain)
        c = b[0]
        if c == Y_ACK:
            b2 = await asyncio.wait_for(reader.readexactly(1), timeout=10)
            return ("ACK", b2[0], b"")
        if c in (Y_NAK, Y_CAN):
            ln = (await asyncio.wait_for(reader.readexactly(1), timeout=10))[0]
            data = b""
            if ln:
                data = await asyncio.wait_for(reader.readexactly(ln), timeout=10)
            return ("NAK" if c == Y_NAK else "CAN", ln, data)
        # anything else: stray byte, keep scanning


async def yapp_send_file(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                         path: Path) -> str:
    """Send one file with YAPP. Returns a user-facing result message."""
    data = path.read_bytes()
    name = path.name

    # Generous timeouts: over 300bd HF the receiver only acks EOF after all
    # queued data has actually drained over RF.
    ack_eof_timeout = max(180.0, len(data) / 15.0)

    try:
        # SI -> expect RR (ACK 01).
        # QtTermTCP only auto-detects YAPP when ENQ 01 arrives as its own
        # 2-byte read, so let any announcement text drain through the node
        # first and send the SI in an isolated packet.
        await writer.drain()
        await asyncio.sleep(2.0)
        writer.write(bytes([Y_ENQ, 0x01]))
        await writer.drain()
        kind, val, _ = await _yapp_wait(reader, 60)
        if kind == "CAN" or kind == "NAK":
            raise YappAbort("Your terminal refused the transfer.")
        if not (kind == "ACK" and val == 0x01):
            raise YappAbort("Unexpected response starting YAPP.")

        # Header -> expect RF (ACK 02) or YappC RT (ACK ACK)
        await asyncio.sleep(FRAME_GAP)
        hdr = name.encode("ascii", "ignore") + b"\x00" + str(len(data)).encode("ascii") + b"\x00"
        writer.write(bytes([Y_SOH, len(hdr)]) + hdr)
        await writer.drain()
        kind, val, _ = await _yapp_wait(reader, 60)
        if kind in ("NAK", "CAN"):
            raise YappAbort("Your terminal declined the file.")
        if kind == "ACK" and val == 0x02:
            use_checksum = False
        elif kind == "ACK" and val == Y_ACK:
            use_checksum = True
        else:
            raise YappAbort("Unexpected response to YAPP header.")

        # Data frames. Pace each frame into its own packet: the node repacks
        # a continuous stream into paclen-size chunks, splitting YAPP frames
        # across packets, and per-packet parsers (e.g. Packet Commander)
        # lose sync on a frame that starts mid-packet.
        await asyncio.sleep(FRAME_GAP)
        sent = 0
        while sent < len(data):
            chunk = data[sent:sent + YAPP_BLOCK]
            pkt = bytes([Y_STX, len(chunk) & 0xFF]) + chunk
            if use_checksum:
                pkt += bytes([sum(chunk) & 0xFF])
            writer.write(pkt)
            sent += len(chunk)
            await writer.drain()
            await asyncio.sleep(FRAME_GAP)

        # EOF -> expect AF (ACK 03)
        writer.write(bytes([Y_ETX, 0x01]))
        await writer.drain()
        await asyncio.sleep(FRAME_GAP)
        kind, val, _ = await _yapp_wait(reader, ack_eof_timeout)
        if kind in ("NAK", "CAN"):
            raise YappAbort("Transfer stopped by your terminal.")
        if not (kind == "ACK" and val == 0x03):
            raise YappAbort("File end was not acknowledged.")

        # EOT -> AT (ACK 04) is a courtesy; don't fail if it never comes
        writer.write(bytes([Y_EOT, 0x01]))
        await writer.drain()
        try:
            await _yapp_wait(reader, 15)
        except asyncio.TimeoutError:
            pass

        mode = "YappC" if use_checksum else "YAPP"
        return f"Transfer complete ({mode}): {name} ({len(data)} bytes)\r\n"

    except YappAbort as e:
        return (f"YAPP: {e}\r\n"
                f"(If your terminal said the file already exists, delete it from your\r\n"
                f"YAPP receive folder and try again.)\r\n" + _bbs_fallback(name))
    except asyncio.TimeoutError:
        return ("YAPP: no response from your terminal - it may not support YAPP.\r\n"
                + _bbs_fallback(name))


# -----------------------------
# Config load (same shape and same error messages as WX)
# -----------------------------

BACKSLASH_HELP = (
    "[CFG] In JSON a single backslash starts an escape code, so a Windows path\n"
    "[CFG] needs its backslashes doubled: \"C:\\\\BPQ\\\\Files\" - or use forward\n"
    "[CFG] slashes: \"C:/BPQ/Files\"."
)


def _config_from_bad_json(raw: str, err: json.JSONDecodeError) -> Dict:
    lines = raw.splitlines()
    print(f"[CFG] {CONFIG_FILE} is not valid JSON: {err.msg}")
    if 1 <= err.lineno <= len(lines):
        print(f"[CFG] line {err.lineno}: {lines[err.lineno - 1]}")
        print("[CFG] " + " " * (len(f"line {err.lineno}: ") + max(0, err.colno - 1)) + "^")
    if "escape" in err.msg.lower():
        print(BACKSLASH_HELP)
    raise SystemExit(1)


def _check_path_setting(cfg: Dict, key: str) -> None:
    val = str(cfg.get(key, ""))
    if any(ch in val for ch in "\t\f\b\n\r"):
        print(f"[CFG] {key} contains a control character - a Windows path with single backslashes.")
        print(f"[CFG] {key!s} = {val!r}")
        print(BACKSLASH_HELP)
        raise SystemExit(1)


def load_config() -> Dict:
    cfg_path = Path(CONFIG_FILE)
    if not cfg_path.exists():
        cfg_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        print(f"[CFG] Created {CONFIG_FILE}. Edit engine URLs and output_dir, then restart.")
        return DEFAULT_CONFIG
    raw = cfg_path.read_text(encoding="utf-8")
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as e:
        cfg = _config_from_bad_json(raw, e)
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
        elif isinstance(v, dict) and isinstance(cfg.get(k), dict):
            for kk, vv in v.items():
                cfg[k].setdefault(kk, vv)
    _check_path_setting(cfg, "output_dir")
    return cfg


# -----------------------------
# BPQ TCP server / session handler
# -----------------------------

class AppContext:
    def __init__(self, cfg: Dict):
        self.cfg = cfg
        self.userdb = UserDB(cfg["storage"]["user_db_file"])
        self.engine = Engine(cfg)
        self.output_dir = Path(cfg["output_dir"])
        self.banner = cfg.get("banner", "BPQ DIRECTIONS SERVICE")


class Session:
    """One connected station: HOME, the current route, and a pending pick list."""

    def __init__(self, ctx: AppContext, reader, writer, callsign: str):
        self.ctx = ctx
        self.reader = reader
        self.writer = writer
        self.callsign = callsign
        self.home: Optional[Dict] = ctx.userdb.get_home(callsign)
        self.route: Optional[RouteDoc] = None
        # A pick list is good for exactly the next command.
        self.pending: Optional[Dict] = None

    @property
    def near_pt(self) -> Optional[Tuple[float, float]]:
        return (self.home["lat"], self.home["lon"]) if self.home else None

    async def resolve(self, q: str) -> Tuple[Optional[Dict], List[Dict]]:
        """-> (item, []) when unambiguous, (None, candidates) when the user must
        choose, (None, []) when nothing matched."""
        cands = await asyncio.to_thread(self.ctx.engine.geocode, q, self.near_pt, 6)
        if not cands:
            return None, []
        if len(cands) == 1 or cands[1].get("score", 99) - cands[0].get("score", 0) >= 1.0:
            return cands[0], []
        return None, cands[:5]

    # ---- commands -------------------------------------------------------

    async def cmd_route(self, arg: str, a: Optional[Dict] = None) -> str:
        m = re.split(r"\s+TO\s+", arg.strip(), maxsplit=1, flags=re.I)
        if a is None:
            if len(m) == 2:
                from_q, to_q = m[0].strip(), m[1].strip()
            else:
                from_q, to_q = "", arg.strip()
            if not to_q:
                return "Usage: ROUTE <from> TO <to>, e.g. ROUTE 33445 TO Boca Raton\r\n"
            if from_q:
                a, cands = await self.resolve(from_q)
                if a is None:
                    if not cands:
                        return f"No place called '{from_q}'. Try zip, grid, 'street, town' or a landmark.\r\n"
                    self.pending = {"verb": "ROUTE", "slot": "from", "cands": cands, "to_q": to_q, "q": from_q}
                    return pick_text(from_q, cands)
            else:
                if not self.home:
                    return "No HOME set, so I need both ends: ROUTE <from> TO <to>, or HOME 33445 first.\r\n"
                a = {"kind": "home", "name": self.home["label"], "lat": self.home["lat"],
                     "lon": self.home["lon"], "grid": self.home["grid"]}
        else:
            to_q = arg
        b, cands = await self.resolve(to_q)
        if b is None:
            if not cands:
                return f"No place called '{to_q}'. Try zip, grid, 'street, town' or a landmark.\r\n"
            self.pending = {"verb": "ROUTE", "slot": "to", "cands": cands, "from_item": a, "q": to_q}
            return pick_text(to_q, cands)
        return await self.do_route(a, b)

    async def do_route(self, a: Dict, b: Dict) -> str:
        try:
            path = await asyncio.to_thread(self.ctx.engine.route, a, b)
        except EngineError as e:
            msg = str(e)
            if "out of bounds" in msg:
                return "One end is outside the map this node carries.\r\n" + self.ctx.engine.map_line() + "\r\n"
            if "Connection between locations not found" in msg or "no route" in msg:
                return "No drivable route between those two points.\r\n"
            return f"Routing engine: {msg}\r\n"
        self.route = RouteDoc(a, b, path, self.ctx.engine.map_line())
        print(f"[ROUTE] {self.callsign}: {short_place(a)} -> {short_place(b)} "
              f"{path['distance'] / MI:.1f}mi {len(self.route.rows)} steps")
        return self.route.render_page(0)

    async def cmd_where(self, arg: str) -> str:
        q = arg.strip()
        if not q:
            return "Usage: WHERE <place>, e.g. WHERE Bethesda Hospital\r\n"
        item, cands = await self.resolve(q)
        if item is None:
            if not cands:
                return f"No place called '{q}'.\r\n"
            self.pending = {"verb": "WHERE", "slot": "where", "cands": cands, "q": q}
            return pick_text(q, cands)
        return where_text(item, self.home)

    async def cmd_near(self, arg: str) -> str:
        q = arg.strip()
        if q:
            item, cands = await self.resolve(q)
            if item is None:
                if not cands:
                    return f"No place called '{q}'.\r\n"
                self.pending = {"verb": "NEAR", "slot": "near", "cands": cands, "q": q}
                return pick_text(q, cands)
        else:
            if not self.home:
                return "Usage: NEAR <place> (or set HOME first: HOME 33445)\r\n"
            item = {"kind": "home", "name": self.home["label"], "lat": self.home["lat"], "lon": self.home["lon"]}
        return await self.do_near(item)

    async def do_near(self, item: Dict) -> str:
        radius = 25.0
        try:
            items = await asyncio.to_thread(self.ctx.engine.near, item["lat"], item["lon"], 12, radius)
        except EngineError as e:
            return f"Gazetteer: {e}\r\n"
        # The centre itself is not news.
        items = [i for i in items if not (i["dist_mi"] < 0.05 and i["name"] == item.get("name"))]
        return near_text(item, items[:12], radius)

    async def cmd_home(self, arg: str) -> str:
        q = arg.strip()
        if not q:
            return "Usage: HOME <zip|grid|place>, e.g. HOME 33445\r\n"
        item, cands = await self.resolve(q)
        if item is None:
            if not cands:
                return f"No place called '{q}'.\r\n"
            self.pending = {"verb": "HOME", "slot": "home", "cands": cands, "q": q}
            return pick_text(q, cands)
        return self.do_home(item)

    def do_home(self, item: Dict) -> str:
        self.home = home_from_item(item)
        self.ctx.userdb.set_home(self.callsign, self.home)
        return home_card(self.home, self.ctx.engine.map_line())

    async def cmd_pick(self, n: int) -> Optional[str]:
        p = self.pending
        self.pending = None
        if not p or not (1 <= n <= len(p["cands"])):
            return None
        item = p["cands"][n - 1]
        slot = p["slot"]
        if slot == "from":
            return await self.cmd_route(p["to_q"], a=item)
        if slot == "to":
            return await self.do_route(p["from_item"], item)
        if slot == "where":
            return where_text(item, self.home)
        if slot == "near":
            return await self.do_near(item)
        if slot == "home":
            return self.do_home(item)
        return None

    async def cmd_yapp(self) -> str:
        if not self.route:
            return "No route yet. ROUTE <from> TO <to> first, then YAPP.\r\n"
        await asyncio.to_thread(prune_old_files, self.ctx.output_dir, int(self.ctx.cfg["file_max_age_hours"]))
        name = await asyncio.to_thread(write_route_file, self.ctx.output_dir, self.callsign,
                                       self.route, self.ctx.banner)
        send(self.writer, f"Sending {name}.\r\nArm your terminal's YAPP receive now...\r\n")
        await self.writer.drain()
        return await yapp_send_file(self.reader, self.writer, self.ctx.output_dir / name)


async def read_line(reader: asyncio.StreamReader, timeout: Optional[float] = None) -> Optional[str]:
    try:
        if timeout:
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        else:
            raw = await reader.readline()
    except (asyncio.TimeoutError, ConnectionResetError, OSError):
        return None
    if not raw:
        return None
    text = raw.decode("utf-8", "ignore")
    if re.search(r"[\x00-\x08\x0b-\x1f\x7f]", text.rstrip("\r\n")):
        print(f"[EDIT] raw input {text.rstrip(chr(13) + chr(10))!r}")
    return apply_line_edits(text).strip()


def apply_line_edits(s: str) -> str:
    """Apply backspace / DEL the way a terminal would, then drop other control
    characters. A phone client that re-flows a word at its wrap column sends
    'neptune' BS BS 'ne'; kept raw, that searched for 'neptunene'."""
    out: List[str] = []
    for ch in s:
        if ch in "\x08\x7f":
            if out:
                out.pop()
        elif ch >= " " or ch == "\t":
            out.append(ch)
    return "".join(out)


async def prompt_for_home(s: Session) -> bool:
    """First visit: ask for HOME as zip or grid. Returns False if the user quits."""
    for _ in range(3):
        send(s.writer, "Enter your HOME zip code or grid square (or Q to quit):\r\n> ")
        await s.writer.drain()
        ans = await read_line(s.reader)
        if ans is None or ans.upper() in EXIT_WORDS:
            return False
        q = ans.strip()
        if not (re.fullmatch(r"\d{5}", q) or is_grid(q)):
            send(s.writer, f"'{q[:20]}' is not a zip or grid. Try 33445 or EL96xl.\r\n")
            continue
        try:
            item, _ = await s.resolve(q)
        except EngineError as e:
            send(s.writer, f"Gazetteer: {e}\r\n")
            return False
        if item is None:
            send(s.writer, f"'{q}' is not in this node's map.\r\n")
            continue
        s.do_home(item)
        return True
    return False


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ctx: AppContext) -> None:
    try:
        callsign = (await read_line(reader, timeout=5.0)) or "UNKNOWN"
        callsign = callsign.upper()
        print(f"[CONN] {callsign}")
        s = Session(ctx, reader, writer, callsign)

        send(writer, "Welcome to DIRECTIONS - offline driving\r\ndirections and place lookup.\r\n")
        if not s.home:
            if not await prompt_for_home(s):
                return
        send(writer, home_card(s.home, ctx.engine.map_line()))
        send(writer, "\r\n" + MENU_LINE + "> ")
        await writer.drain()

        show_menu = False
        while True:
            line = await read_line(reader)
            if line is None:
                return
            # A blank line under the command the operator just typed, so the
            # answer reads as a separate block. See NODE-APP-STYLE.md.
            send(writer, "\r\n")
            up = line.strip().upper()
            verb, _, arg = line.strip().partition(" ")
            verb = verb.upper()
            had_pending = s.pending is not None

            try:
                if up in EXIT_WORDS:
                    send(writer, "73!\r\n")
                    await writer.drain()
                    return

                elif up == "":
                    s.pending = None
                    send(writer, home_card(s.home, ctx.engine.map_line()))

                elif had_pending and (re.fullmatch(r"\d", up) or
                                      (verb == s.pending["verb"] and re.fullmatch(r"\d", arg.strip()))):
                    n = int(up if re.fullmatch(r"\d", up) else arg.strip())
                    reply = await s.cmd_pick(n)
                    send(writer, reply if reply is not None else f"'{line.strip()[:20]}' is not one of the numbers.\r\n")

                elif verb in ROUTE_VERBS:
                    s.pending = None
                    send(writer, await s.cmd_route(arg))

                elif verb == "WHERE":
                    s.pending = None
                    send(writer, await s.cmd_where(arg))

                elif verb == "NEAR":
                    s.pending = None
                    send(writer, await s.cmd_near(arg))

                elif verb == "HOME":
                    s.pending = None
                    send(writer, await s.cmd_home(arg))

                elif up in MORE_WORDS + BACK_WORDS + TOP_WORDS + ALL_WORDS:
                    s.pending = None
                    if not s.route:
                        send(writer, "No route to page. ROUTE <from> TO <to> first.\r\n")
                    elif up in MORE_WORDS:
                        send(writer, s.route.next_page() or (
                            "End of route. BACK or TOP to page up.\r\n" if s.route.pages > 1
                            else "That is the whole route.\r\n"))
                    elif up in BACK_WORDS:
                        send(writer, s.route.prev_page() or "Already at the first page.\r\n")
                    elif up in TOP_WORDS:
                        send(writer, s.route.render_page(0))
                    else:
                        send(writer, s.route.rest() or "End of route already shown.\r\n")

                elif up in ("YAPP", "DL", "YAPP TXT", "DL TXT"):
                    s.pending = None
                    send(writer, await s.cmd_yapp())

                elif up in HELP_WORDS:
                    s.pending = None
                    send(writer, HELP_TEXT)
                    show_menu = True

                else:
                    s.pending = None
                    # Same wording as the other apps, quoting what was not
                    # understood rather than just saying "Unknown command".
                    send(writer, f"'{line.strip()[:20]}' is not a command.\r\n")
                    show_menu = True
            except EngineError as e:
                send(writer, f"Routing engine: {e}\r\n")

            # The menu on demand, not after every reply. See NODE-APP-STYLE.md.
            menu = MENU_LINE if (MENU_EVERY or show_menu) else ""
            show_menu = False
            send(writer, "\r\n" + menu + "> ")
            await writer.drain()

    except (ConnectionResetError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def run_server() -> None:
    cfg = load_config()
    apply_display_config(cfg)
    ctx = AppContext(cfg)
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    print(f"[CFG] Route files (YAPP) will be written to: {ctx.output_dir.resolve()}")
    print(f"[CFG] Engine: GraphHopper {ctx.engine.gh}  gazetteer {ctx.engine.gz}")
    m = ctx.engine.meta()
    if m:
        print(f"[CFG] {ctx.engine.map_line()}  ({m.get('entries', '?')} gazetteer entries)")
    else:
        print("[CFG] WARNING: gazetteer not reachable right now - the app will keep trying per request.")

    host = cfg["bpq_app"]["listen_host"]
    port = int(cfg["bpq_app"]["listen_port"])
    server = await asyncio.start_server(lambda r, w: handle_client(r, w, ctx), host, port)
    print(f"[ROUTE] Listening on {host}:{port}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        print("Shutting down.")
