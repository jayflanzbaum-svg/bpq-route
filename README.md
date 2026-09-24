# bpq-route

Offline driving directions, place lookup and "what is near here" for BPQ32
packet radio nodes. No internet involved: the map is an OpenStreetMap extract
served by GraphHopper and a small gazetteer on a box on your LAN (OffgridAI
ships both; see [Where the map comes from](#where-the-map-comes-from)).

Users connect to your node, type `ROUTE`, set their HOME once (zip or grid),
and get a turn list built for a 42-column packet terminal: about 40 bytes a
turn, a 240-mile trip in a dozen lines.

```
> ROUTE Jupiter TO Key West

RT 241.3mi ~5h09
Jupiter FL > Key West FL
 #  TURN  ROAD                       MI
 1  GO    W Indiantown Rd/SR-706       <.1
 2  KL    unnamed rd                   0.1
 3  HL    W Indiantown Rd/SR-706       0.1
 4  R     Old Dixie Hwy/SR-811         5.8
 5  KR    to I-95 S                   32.0
 6  KL    I-95 EXPR                   30.5
 7  KR    to Ives Dairy Rd             169
 8  KR    US-1                         <.1
 9  SR    N Roosevelt Blvd/US-1        3.4
10  R     Duval St                     0.3
11  L     Southard St                  <.1
12  END   arrive                       <.1
1-12 of 12 - end of route
Map: OSM Florida 2026-09-24 via GraphHopper
```

## Commands

| command | what it does |
|---|---|
| `<ENTER>` | your HOME card: grid square, lat/lon, map date |
| `ROUTE <from> TO <to>` | driving directions |
| `ROUTE <to>` | from HOME to `<to>` |
| `WHERE <place>` | grid square, lat/lon, distance and bearing from HOME |
| `NEAR <place>` / `NEAR` | nearest towns and landmarks (hospitals, fire, schools, parks, towers...) |
| `HOME <place>` | set HOME for your callsign; remembered between connects |
| `MORE` | next page of a long route (14 turns a screen) |
| `YAPP` | send the current route to your PC as a `.txt` (YAPP / YappC) |
| `HELP`, `Q` | the usual |

A `<place>` is any of: a US zip (`33445`), a Maidenhead grid (`EL96xl`),
`lat,lon`, a town (`Boca Raton`), a street with its town (`Congress Ave,
Delray Beach` - the comma is optional), or a named landmark (`Bethesda
Hospital`, `Palm Beach International Airport`). When a name matches more than
one thing you get a numbered list and reply with the number:

```
> WHERE Bethesda Hospital

Which 'Bethesda Hospital'?
1 Bethesda Hospital East, Boynton Beach FL
  (hospital)
2 Baptist Health Bethesda Hospital West,
  Boynton Beach FL (hospital)
Reply with a number 1-2.
```

Turn codes: `L` `R` left/right, `SL` `SR` slight, `HL` `HR` hard, `KL` `KR`
keep left/right (ramps and forks), `RB2` roundabout exit 2, `U` U-turn, `GO`
continue, `END` arrive. Street types are abbreviated the way a road sign
does (`Ave`, `Blvd`, `NE`, `SR-806`). House numbers are best-effort:
OpenStreetMap address coverage in the US is patchy, so aim for street + town
or a landmark.

## Requirements

- Python 3.10+ on the node PC. **No pip packages** - standard library only.
- A BPQ32 node with a Telnet port (BPQ32 6.0.20.1 or later for in-session YAPP).
- A GraphHopper server and the OffgridAI gazetteer reachable over your LAN
  (next section). Both are plain HTTP; the app never needs the internet.

## Where the map comes from

The app itself carries no map. It asks two services:

| service | port | job |
|---|---|---|
| GraphHopper 11 | 8989 | the route: `/route?point=lat,lon&point=lat,lon&profile=car` |
| OffgridAI gazetteer | 8991 | names to coordinates: `/geocode?q=Congress Ave, Delray Beach`, plus `/near` and `/reverse` |

**Easiest:** an [OffgridAI](https://github.com/jayflanzbaum-svg/OffgridAI) box.
Its `docker-compose.yml` runs both from one OpenStreetMap state extract
(`routing/` in that repo: config, gazetteer builder, `refresh-map.sh`).
Point `engine.graphhopper_url` / `engine.gazetteer_url` at it and you are done.

**Without OffgridAI:** GraphHopper is a single Java jar and the gazetteer is a
Python script plus a SQLite file; both run on Windows too. Copy the
`routing/` folder from the OffgridAI repo, follow its README to build the
gazetteer from a Geofabrik extract, and run the two servers on the node PC
(or anywhere on the LAN).

## Install (Windows, no Python experience needed)

1. **Install Python.** [python.org/downloads](https://www.python.org/downloads/),
   run the installer, and **check "Add python.exe to PATH"** on the first
   screen before clicking Install.
2. **Download this app.** Green **Code** button → **Download ZIP**, unzip
   somewhere easy, e.g. `C:\bpq-route`. (`git clone` works too and makes
   updates a one-line `git pull`.)
3. **Open a terminal in that folder.** In File Explorer open the folder,
   click in the address bar, type `cmd`, press Enter.
4. **Run it once** to generate `route_config.json`:
   ```
   python bpq_route.py
   ```
   Press `Ctrl+C` to stop it.
5. **Edit `route_config.json`** (right-click → Open with → Notepad):
   - `engine.graphhopper_url` and `engine.gazetteer_url` — your OffgridAI
     box (or wherever the two services run), e.g. `http://192.168.1.50:8989`
     and `http://192.168.1.50:8991`.
   - `output_dir` — your BBS Files folder, for the YAPP download. **A Windows
     path needs its backslashes doubled** (`"C:\\Users\\<you>\\AppData\\Roaming\\BPQ32\\BPQMailChat\\Files"`)
     or use forward slashes. The app prints the folder it is using at startup.
   - `banner` — the station line at the bottom of saved routes.
   - `bpq_app.listen_port` — must match the CMDPORT entry below (default 63053).
6. **Add the app to `BPQ32.cfg`.** In your Telnet port `CONFIG` block, add the
   port to `CMDPORT` (space-separated; note its zero-based position - that is
   the HOST number):
   ```
   CMDPORT=63053
   ```
   In the **APPLICATIONS** section (adjust the number, HOST index and your
   callsign/alias):
   ```
   APPLICATION 7,ROUTE,C 7 HOST 0 S TRANS,MYCALL-16,NODERT,255
   ```
   `C 7` is your Telnet port; `HOST 0` the CMDPORT position; `S` returns the
   user to the node when the app exits; **`TRANS` is required for YAPP**
   (binary mode so the transfer bytes pass through untouched). BPQ sends the
   connecting callsign to the app automatically.
7. **Restart BPQ32, start the app** (`python bpq_route.py`, leave the window
   open, or put that line in a `.bat` and add it to startup). Connect to your
   node and type `ROUTE`.

## Testing without a node

`tools/route_probe.py` connects to the app exactly the way BPQ does (callsign
first, then commands) and prints what a station would see:

```
python tools/route_probe.py N0CALL "ROUTE 33445 TO Boca Raton" MORE Q
```

Give it `host:port` as the first argument for a non-default listen port.

## Troubleshooting

- **`Routing engine: engine unreachable`** — the app cannot reach GraphHopper
  or the gazetteer. Check the two URLs in `route_config.json` from a browser on
  the node PC: `http://<box>:8989/health` should say `OK` and
  `http://<box>:8991/health` should return JSON with the map date.
- **`One end is outside the map this node carries`** — the extract is one
  state. Neighbouring-state trips need a bigger extract on the box
  (`routing/refresh-map.sh` in OffgridAI takes any Geofabrik URL).
- **`No drivable route between those two points`** — usually an island or a
  private road. Try the nearest town or a street instead.
- **A wall of red `Traceback` ending in `JSONDecodeError`** — a typo in
  `route_config.json`, almost always single backslashes in `output_dir`.
  The app prints the offending line with a `^` under it.
- **YAPP says the file already exists** — the receiver refuses duplicates;
  each file name carries the time, so just try again a minute later, or delete
  it from your YAPP receive folder.

## House style

Output follows the shared node-app style used by
[bpq-wx](https://github.com/jayflanzbaum-svg/bpq-wx) and
[bpq-repeatersearch](https://github.com/jayflanzbaum-svg/bpq-repeatersearch):
42-column output (Mobile Packet Commander is 43), tables that fit by
construction, units in headers, the same `Q QUIT EXIT BYE NODE` / `HELP` /
`MORE` vocabulary, the menu on connect and after `HELP` rather than after every
reply, and every byte leaving through one `send()`.

## License

MIT. Map data © OpenStreetMap contributors (ODbL). Routing by
[GraphHopper](https://www.graphhopper.com/) (Apache 2.0).
