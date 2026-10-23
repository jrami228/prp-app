#!/usr/bin/env python3
"""
Rebuild the FDEP-derived stored data files in ../data/ from official FDEP sources.

Requires only Python 3.8+ (standard library, no installs). Run from the repo root:

    python3 scripts/refresh_data.py                 # rebuild all FDEP datasets
    python3 scripts/refresh_data.py --only icr      # rebuild one dataset
    python3 scripts/refresh_data.py --out /tmp/x    # write somewhere else to compare first
    python3 scripts/refresh_data.py --with-caps     # also pull Cap Amounts from the STCM
                                                    # PRP Eligibility Report (statewide, ~33 MB)

Datasets rebuilt (all FDEP sources):
  facilities.json     FDEP Open Data GIS, DWM_STCM layer 5 "Storage Tank Contamination
                      Monitoring (STCM)", COUNTY=13 AND CONTAM='YES'
  eligibility.json    FDEP Open Data GIS, DWM_STCM layer 2 "Petroleum Contamination
                      Monitoring (PCTS) Discharges from STCM", COUNTY='MIAMI-DADE'
                      (+ optional Cap Amounts from STCM PRP Eligibility Report)
  shared-address.json FDEP Open Data GIS, CLEANUP_SP layer 0 "DEP Cleanup Sites",
                      CC2_COUNTY_ID=13, grouped by ADDRESS1 + ZIP5
  icr.json            FDEP Open Data GIS, DWM_WASTE_ICR_BACKG layer 12 "Florida
                      Institutional Controls Registry", Miami-Dade, keyed by STCM FACID

NOT rebuilt (Miami-Dade DERM exports, external to FDEP; update by hand):
  ut-mapping.json, folios.json, facid-tasks.json

meta.json is updated with the run date for every dataset rebuilt.
"""
import argparse
import datetime as dt
import html.parser
import json
import os
import sys
import time
import urllib.parse
import urllib.request

GIS = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData"
PCTS = GIS + "/DWM_STCM/MapServer/2"
STCM = GIS + "/DWM_STCM/MapServer/5"
CLEANUP = GIS + "/CLEANUP_SP/MapServer/0"
ICR = GIS + "/DWM_WASTE_ICR_BACKG/MapServer/12"
PRP_ELIG_REPORT = "https://prodapps.dep.state.fl.us/www_stcm/reports/PrpEligibility/Prp_Eligibility"
UA = {"User-Agent": "PRP-Records-Search-refresh/1.0"}

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.normpath(os.path.join(HERE, "..", "data"))


def http(url, data=None, timeout=120, tries=4):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # FDEP servers are occasionally flaky ("no healthy upstream")
            if i == tries - 1:
                raise
            print(f"  retry {i + 1} after error: {e}", file=sys.stderr)
            time.sleep(5 * (i + 1))


def gis_query_all(layer, where, fields):
    """Page through an ArcGIS MapServer layer query (resultOffset paging)."""
    meta = json.loads(http(layer + "?f=json"))
    page = int(meta.get("maxRecordCount") or 1000)
    page = min(page, 2000)
    out, offset = [], 0
    while True:
        q = {
            "where": where, "outFields": ",".join(fields), "returnGeometry": "false",
            "orderByFields": "OBJECTID", "resultOffset": offset, "resultRecordCount": page, "f": "json",
        }
        d = json.loads(http(layer + "/query?" + urllib.parse.urlencode(q)))
        if "error" in d:
            raise RuntimeError(f"GIS error for {layer}: {d['error']}")
        feats = [f["attributes"] for f in d.get("features", [])]
        out.extend(feats)
        if len(feats) < page and not d.get("exceededTransferLimit"):
            break
        offset += len(feats)
        if not feats:
            break
    return out


def ms_to_ymd(ms):
    if ms is None:
        return ""
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y/%m/%d")


def s(v):
    return "" if v is None else str(v).strip()


def build_facilities():
    rows = gis_query_all(STCM, "COUNTY=13 AND CONTAM='YES'", ["FAC_ID", "NAME", "ADDRESS1"])
    seen, out = set(), []
    for r in rows:
        f = s(r["FAC_ID"])
        if not f or f in seen:
            continue
        seen.add(f)
        out.append({"f": f, "n": s(r["NAME"]).upper(), "a": s(r["ADDRESS1"]).upper()})
    out.sort(key=lambda x: (x["n"], x["f"]))
    return out


class _TableParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell, self._in = [], None, None, False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell, self._in = [], True

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in:
            self._row.append(" ".join("".join(self._cell).split()))
            self._in = False
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._in:
            self._cell.append(data)


def fetch_caps():
    """(facid, 'YYYY/MM/DD') -> cap string, Miami-Dade only, from the STCM PRP Eligibility Report."""
    print("  downloading STCM PRP Eligibility Report (statewide, large)...")
    raw = http(PRP_ELIG_REPORT, {"fac": "", "prog": "0", "query_program": "ALL"}, timeout=600)
    p = _TableParser()
    p.feed(raw.decode("utf-8", "replace"))
    header = next((r for r in p.rows if "Facility ID" in r and "Cap Amount" in r), None)
    if not header:
        raise RuntimeError("PRP Eligibility Report layout not recognized")
    ix = {name: header.index(name) for name in header}
    caps = {}
    for r in p.rows:
        if len(r) != len(header) or r is header:
            continue
        if r[ix["County"]] != "13":
            continue
        cap = r[ix["Cap Amount"]]
        if not cap or cap in ("$0.00", "$0"):
            continue
        m, d, y = (r[ix["Discharge Date"]].split("/") + ["", "", ""])[:3]
        caps[(r[ix["Facility ID"]], f"{y}/{m}/{d}")] = cap
    return caps


def build_eligibility(with_caps):
    rows = gis_query_all(PCTS, "COUNTY='MIAMI-DADE'", [
        "FACILITY_ID", "DISCHARGE_DATE", "ELIGIBILITY_PROGRAM", "DISCHARGE_SCORE",
        "ELIGIBILITY", "DISCHARGE_CLEANUP_STATUS"])
    caps = {}
    if with_caps:
        try:
            caps = fetch_caps()
        except Exception as e:
            print(f"  WARNING: cap amounts skipped ({e})", file=sys.stderr)
    out = {}
    for r in rows:
        f = s(r["FACILITY_ID"])
        if not f:
            continue
        date = ms_to_ymd(r["DISCHARGE_DATE"])
        row = [date, s(r["ELIGIBILITY_PROGRAM"]), s(r["DISCHARGE_SCORE"]), s(r["ELIGIBILITY"]),
               s(r["DISCHARGE_CLEANUP_STATUS"])]
        cap = caps.get((f, date))
        if cap:
            row.append(cap)
        out.setdefault(f, []).append(row)
    for f in out:
        out[f].sort(key=lambda x: x[0], reverse=True)
    return out


def build_shared_address():
    rows = gis_query_all(CLEANUP, "CC2_COUNTY_ID=13", [
        "SOURCE_DATABASE_NAME", "SOURCE_DATABASE_ID", "BUSINESS_NAME", "ADDRESS1", "ZIP5", "DOCUMENTS"])
    groups = {}
    for r in rows:
        addr = s(r["ADDRESS1"]).upper()
        if not addr:
            continue
        groups.setdefault((addr, s(r["ZIP5"])), []).append(r)
    out = {}
    for (addr, _zip), sites in groups.items():
        ids = {s(x["SOURCE_DATABASE_ID"]) for x in sites}
        if len(ids) < 2:
            continue
        lst, seen = [], set()
        for x in sites:
            sid = s(x["SOURCE_DATABASE_ID"])
            if sid in seen:
                continue
            seen.add(sid)
            lst.append({"id": sid, "name": s(x["BUSINESS_NAME"]), "db": s(x["SOURCE_DATABASE_NAME"]),
                        "docs": s(x["DOCUMENTS"])})
        for x in lst:
            out[x["id"]] = {"addr": addr, "sites": lst}
    return out


def build_icr():
    fields = ["BOUNDARY_CONTROL_ID", "BOUNDARY_CONTROL_NAME", "BOUNDARY_MECHANISM_TYPES",
              "BOUNDARY_MECHANISM_EFF_DATES", "BOUNDARY_MECHANISM_BOOK_NUMS", "BOUNDARY_MECHANISM_PAGE_NUMS",
              "BOUNDARY_MECHANISM_INSTR_NUMS", "BOUNDARY_RESTRICTIONS", "BOUNDARY_CONTAMINATED_MEDIA",
              "BOUNDARY_ENGINEERING_CONTROLS", "ALL_FACILITY_IDS", "PRIMARY_SITE_DOCUMENTS"]
    rows = gis_query_all(ICR, "BOUNDARY_MECHANISM_COUNTIES LIKE '%Miami-Dade%'", fields)
    out = {}
    for r in rows:
        rec = icr_record(r)
        for token in s(r["ALL_FACILITY_IDS"]).replace(";", ",").split(","):
            token = token.strip()
            if token.upper().startswith("STCM-"):
                out.setdefault(token[5:], []).append(rec)
    return out


def icr_record(r):
    # Same shape the app builds from a live query (see js/app.js icrFromAttrs).
    return {
        "id": s(r.get("BOUNDARY_CONTROL_ID")), "name": s(r.get("BOUNDARY_CONTROL_NAME")),
        "types": s(r.get("BOUNDARY_MECHANISM_TYPES")), "effDates": s(r.get("BOUNDARY_MECHANISM_EFF_DATES")),
        "book": s(r.get("BOUNDARY_MECHANISM_BOOK_NUMS")), "page": s(r.get("BOUNDARY_MECHANISM_PAGE_NUMS")),
        "instr": s(r.get("BOUNDARY_MECHANISM_INSTR_NUMS")), "restrictions": s(r.get("BOUNDARY_RESTRICTIONS")),
        "media": s(r.get("BOUNDARY_CONTAMINATED_MEDIA")), "eng": s(r.get("BOUNDARY_ENGINEERING_CONTROLS")),
        "docs": s(r.get("PRIMARY_SITE_DOCUMENTS")),
    }


BUILDERS = {
    "facilities": ("facilities.json", "FDEP GIS DWM_STCM/5 (STCM), COUNTY=13 AND CONTAM='YES'"),
    "eligibility": ("eligibility.json", "FDEP GIS DWM_STCM/2 (PCTS Discharges), COUNTY='MIAMI-DADE'"),
    "shared": ("shared-address.json", "FDEP GIS CLEANUP_SP/0 (DEP Cleanup Sites), CC2_COUNTY_ID=13"),
    "icr": ("icr.json", "FDEP GIS DWM_WASTE_ICR_BACKG/12 (Florida Institutional Controls Registry), Miami-Dade"),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="facilities,eligibility,shared,icr",
                    help="comma list of: " + ",".join(BUILDERS))
    ap.add_argument("--out", default=DEFAULT_OUT, help="output folder (default: ../data)")
    ap.add_argument("--with-caps", action="store_true", help="add Cap Amounts from the STCM PRP Eligibility Report")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    today = dt.date.today()
    meta_path = os.path.join(a.out, "meta.json")
    try:
        meta = json.load(open(meta_path))
    except Exception:
        meta = {"datasets": {}}
    meta.setdefault("datasets", {})
    for key in [k.strip() for k in a.only.split(",") if k.strip()]:
        if key not in BUILDERS:
            sys.exit(f"unknown dataset '{key}'")
        fname, src = BUILDERS[key]
        print(f"Building {fname} ...")
        if key == "eligibility":
            data = build_eligibility(a.with_caps)
        else:
            data = {"facilities": build_facilities, "shared": build_shared_address, "icr": build_icr}[key]()
        with open(os.path.join(a.out, fname), "w") as fh:
            json.dump(data, fh, separators=(",", ":"))
        print(f"  wrote {fname}: {len(data)} records")
        meta["datasets"][fname] = {"source": src, "asOf": today.strftime("%m/%d/%Y")}
    # Overall date shown on screen = oldest dataset date (most conservative).
    dates = [dt.datetime.strptime(v["asOf"], "%m/%d/%Y").date() for v in meta["datasets"].values() if v.get("asOf")]
    if dates:
        meta["dataDate"] = min(dates).strftime("%m/%d/%Y")
    meta["generated"] = today.isoformat()
    with open(meta_path, "w") as fh:
        json.dump(meta, fh, indent=2)
    print("Done. meta.json dataDate =", meta.get("dataDate"))


if __name__ == "__main__":
    main()
