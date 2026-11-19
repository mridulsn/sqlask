"""Build a disguised copy of Chinook for a memorisation-proof eval.

Chinook is one of the most widely used sample databases, so models know its
table names, its values ('Hip Hop/Rap', 'USA') and even some of its answers.
This script rebuilds the same rows under a "media library" vocabulary:

  - every table and column is renamed (Track -> catalog_item, Invoice -> checkout, ...)
  - every date is shifted by a fixed offset that is not a whole number of years
    and has a time-of-day part, so remembered year totals are wrong and
    `BETWEEN '2019-01-01' AND '2019-12-31'` misses rows on the last day
  - prices are scaled by 75 (a different currency); checkout totals are
    recomputed from their lines so they still add up
  - track length is stored in whole seconds, file size in kilobytes
  - some categorical values are replaced with less obvious ones
    ('Hip Hop/Rap' -> 'Urban / Rap', 'USA' -> 'US', 'Sales Support Agent' ->
    'Customer Advisor'), so hedged guesses such as LIKE '%hip%hop%' fail

The build is deterministic: same source file in, same rows out.

    uv run sqlask-disguise            # writes web/data/medialib.sqlite
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import tempfile
from pathlib import Path

from .db import DEFAULT_DB, ROOT

DISGUISED_DB = ROOT / "web" / "data" / "medialib.sqlite"

DATE_OFFSET = dt.timedelta(days=2963, hours=9, minutes=30)
PRICE_SCALE = 75
DATE_FMT = "%Y-%m-%d %H:%M:%S"

TABLE_NAMES = {
    "Artist": "creator", "Album": "collection", "Track": "catalog_item", "Genre": "style",
    "MediaType": "file_format", "Playlist": "shelf", "PlaylistTrack": "shelf_item",
    "Customer": "member", "Employee": "staff", "Invoice": "checkout",
    "InvoiceLine": "checkout_line",
}

STYLE_LABELS = {
    "Rock": "Rock & Indie", "Latin": "Latin American", "Alternative & Punk": "Punk / Alt",
    "TV Shows": "Television", "Classical": "Orchestral", "Hip Hop/Rap": "Urban / Rap",
    "R&B/Soul": "Soul & R&B", "Rock And Roll": "Rock-n-Roll", "Sci Fi & Fantasy": "Speculative TV",
    "Electronica/Dance": "Dance / Electronic", "Heavy Metal": "Metal (Classic)",
    "Easy Listening": "Lounge", "Soundtrack": "Film Score", "Bossa Nova": "Bossa",
    "Science Fiction": "Speculative Film", "Drama": "Drama Series", "Comedy": "Comedy Series",
    "Alternative": "Alt",
}
FORMAT_LABELS = {
    "MPEG audio file": "mp3", "Protected AAC audio file": "aac-drm",
    "Protected MPEG-4 video file": "m4v-drm", "Purchased AAC audio file": "aac-store",
    "AAC audio file": "aac",
}
ROLE_TITLES = {
    "General Manager": "Store Director", "Sales Manager": "Head of Sales",
    "Sales Support Agent": "Customer Advisor", "IT Manager": "Systems Lead",
    "IT Staff": "Systems Technician",
}
NATIONS = {
    "USA": "US", "United Kingdom": "GB", "Brazil": "Brasil", "Germany": "Deutschland",
    "Czech Republic": "Czechia", "Netherlands": "NL",
}

DDL = """
CREATE TABLE creator (
  creator_id INTEGER PRIMARY KEY,
  creator_name TEXT
);
CREATE TABLE collection (
  collection_id INTEGER PRIMARY KEY,
  collection_title TEXT NOT NULL,
  creator_id INTEGER NOT NULL REFERENCES creator(creator_id)
);
CREATE TABLE style (
  style_id INTEGER PRIMARY KEY,
  style_label TEXT
);
CREATE TABLE file_format (
  format_id INTEGER PRIMARY KEY,
  format_label TEXT
);
CREATE TABLE catalog_item (
  item_id INTEGER PRIMARY KEY,
  item_title TEXT NOT NULL,
  collection_id INTEGER REFERENCES collection(collection_id),
  format_id INTEGER NOT NULL REFERENCES file_format(format_id),
  style_id INTEGER REFERENCES style(style_id),
  credits TEXT,
  duration_sec INTEGER NOT NULL,
  size_kb INTEGER,
  price REAL NOT NULL
);
CREATE TABLE shelf (
  shelf_id INTEGER PRIMARY KEY,
  shelf_title TEXT
);
CREATE TABLE shelf_item (
  shelf_id INTEGER NOT NULL REFERENCES shelf(shelf_id),
  item_id INTEGER NOT NULL REFERENCES catalog_item(item_id),
  PRIMARY KEY (shelf_id, item_id)
);
CREATE TABLE staff (
  staff_id INTEGER PRIMARY KEY,
  family_name TEXT NOT NULL,
  given_name TEXT NOT NULL,
  role_title TEXT,
  manager_id INTEGER REFERENCES staff(staff_id),
  born_on TEXT,
  joined_on TEXT,
  street TEXT,
  town TEXT,
  province TEXT,
  nation TEXT,
  postcode TEXT,
  phone TEXT,
  fax TEXT,
  email TEXT
);
CREATE TABLE member (
  member_id INTEGER PRIMARY KEY,
  given_name TEXT NOT NULL,
  family_name TEXT NOT NULL,
  organisation TEXT,
  street TEXT,
  town TEXT,
  province TEXT,
  nation TEXT,
  postcode TEXT,
  phone TEXT,
  fax TEXT,
  email TEXT NOT NULL,
  advisor_id INTEGER REFERENCES staff(staff_id)
);
CREATE TABLE checkout (
  checkout_id INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES member(member_id),
  checked_out_at TEXT NOT NULL,
  bill_street TEXT,
  bill_town TEXT,
  bill_province TEXT,
  bill_nation TEXT,
  bill_postcode TEXT,
  amount REAL NOT NULL
);
CREATE TABLE checkout_line (
  line_id INTEGER PRIMARY KEY,
  checkout_id INTEGER NOT NULL REFERENCES checkout(checkout_id),
  item_id INTEGER NOT NULL REFERENCES catalog_item(item_id),
  price_each REAL NOT NULL,
  qty INTEGER NOT NULL
);
CREATE INDEX ix_collection_creator ON collection(creator_id);
CREATE INDEX ix_item_collection ON catalog_item(collection_id);
CREATE INDEX ix_item_style ON catalog_item(style_id);
CREATE INDEX ix_item_format ON catalog_item(format_id);
CREATE INDEX ix_shelf_item_item ON shelf_item(item_id);
CREATE INDEX ix_member_advisor ON member(advisor_id);
CREATE INDEX ix_staff_manager ON staff(manager_id);
CREATE INDEX ix_checkout_member ON checkout(member_id);
CREATE INDEX ix_line_checkout ON checkout_line(checkout_id);
CREATE INDEX ix_line_item ON checkout_line(item_id);
"""


def shift_date(value: str | None) -> str | None:
    if value is None:
        return None
    return (dt.datetime.strptime(value, DATE_FMT) + DATE_OFFSET).strftime(DATE_FMT)


def scale_price(value) -> float:
    # 0.99 -> 74.25, 1.99 -> 149.25; round on cents to avoid float drift
    return round(round(float(value) * 100) * PRICE_SCALE / 100, 2)


def nation(value: str | None) -> str | None:
    return NATIONS.get(value, value)


def _rows(src: sqlite3.Connection, sql: str):
    return src.execute(sql).fetchall()


def build(src_path: str | Path = DEFAULT_DB, out_path: str | Path = DISGUISED_DB) -> Path:
    src_path, out_path = Path(src_path), Path(out_path)
    src = sqlite3.connect(f"{src_path.as_uri()}?mode=ro", uri=True)
    with tempfile.TemporaryDirectory(prefix="sqlask-disguise-") as tmp:
        work = sqlite3.connect(Path(tmp) / "work.sqlite")
        work.executescript(DDL)
        ins = lambda table, rows: work.executemany(  # noqa: E731
            f"INSERT INTO {table} VALUES ({','.join('?' * len(rows[0]))})", rows) if rows else None

        ins("creator", _rows(src, "SELECT ArtistId, Name FROM Artist ORDER BY 1"))
        ins("collection", _rows(src, "SELECT AlbumId, Title, ArtistId FROM Album ORDER BY 1"))
        ins("style", [(i, STYLE_LABELS.get(n, n)) for i, n in
                      _rows(src, "SELECT GenreId, Name FROM Genre ORDER BY 1")])
        ins("file_format", [(i, FORMAT_LABELS.get(n, n)) for i, n in
                            _rows(src, "SELECT MediaTypeId, Name FROM MediaType ORDER BY 1")])
        ins("catalog_item", [
            (tid, name, alb, mt, gen, comp, (ms + 500) // 1000,
             None if b is None else (b + 512) // 1024, scale_price(price))
            for tid, name, alb, mt, gen, comp, ms, b, price in _rows(src, """
                SELECT TrackId, Name, AlbumId, MediaTypeId, GenreId, Composer,
                       Milliseconds, Bytes, UnitPrice FROM Track ORDER BY 1""")])
        ins("shelf", _rows(src, "SELECT PlaylistId, Name FROM Playlist ORDER BY 1"))
        ins("shelf_item", _rows(src, "SELECT PlaylistId, TrackId FROM PlaylistTrack ORDER BY 1, 2"))
        ins("staff", [
            (eid, last, first, ROLE_TITLES.get(title, title), boss, shift_date(born),
             shift_date(hired), addr, city, state, nation(country), pc, phone, fax, email)
            for eid, last, first, title, boss, born, hired, addr, city, state, country, pc, phone,
            fax, email in _rows(src, """
                SELECT EmployeeId, LastName, FirstName, Title, ReportsTo, BirthDate, HireDate,
                       Address, City, State, Country, PostalCode, Phone, Fax, Email
                FROM Employee ORDER BY 1""")])
        ins("member", [
            (cid, first, last, company, addr, city, state, nation(country), pc, phone, fax,
             email, rep)
            for cid, first, last, company, addr, city, state, country, pc, phone, fax, email, rep
            in _rows(src, """
                SELECT CustomerId, FirstName, LastName, Company, Address, City, State, Country,
                       PostalCode, Phone, Fax, Email, SupportRepId FROM Customer ORDER BY 1""")])

        lines = [(lid, inv, tid, scale_price(price), qty) for lid, inv, tid, price, qty in _rows(
            src, "SELECT InvoiceLineId, InvoiceId, TrackId, UnitPrice, Quantity "
                 "FROM InvoiceLine ORDER BY 1")]
        amounts: dict[int, int] = {}
        for _lid, inv, _tid, price, qty in lines:  # sum in cents, exact
            amounts[inv] = amounts.get(inv, 0) + round(price * 100) * qty
        ins("checkout", [
            (iid, cid, shift_date(date), addr, city, state, nation(country), pc,
             amounts.get(iid, 0) / 100)
            for iid, cid, date, addr, city, state, country, pc in _rows(src, """
                SELECT InvoiceId, CustomerId, InvoiceDate, BillingAddress, BillingCity,
                       BillingState, BillingCountry, BillingPostalCode FROM Invoice ORDER BY 1""")])
        ins("checkout_line", lines)
        work.commit()

        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.exists():
            out_path.unlink()
        work.execute("VACUUM INTO ?", (str(out_path),))
        work.close()
    src.close()
    return out_path


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Build the disguised Chinook copy")
    p.add_argument("--src", default=str(DEFAULT_DB))
    p.add_argument("--out", default=str(DISGUISED_DB))
    args = p.parse_args(argv)
    out = build(args.src, args.out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
