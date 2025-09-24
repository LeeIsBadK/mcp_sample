#!/usr/bin/env python3
"""
Hotel MCP Server (SQLite-backed)

Tools:
- list_rooms(available_only: bool = True)
- create_user(name: str, age: int, num_guests: int)
- book_room(room_number: str, name: str, age: int, num_guests: int,
            check_in: str (YYYY-MM-DD), check_out: str (YYYY-MM-DD))
- get_reservations(room_number: Optional[str] = None, name: Optional[str] = None)
- cancel_reservation(reservation_id: int)
- checkout(reservation_id: int)

Run:
  python mcp_hotel_server.py

Then register this server in your MCP-compatible client (or your HTTP bridge)
and call the tools.
"""

from __future__ import annotations
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple

from mcp.server.fastmcp import FastMCP

DB_PATH = "../test1.db"
mcp = FastMCP("Hotel MCP Server")
_tool = mcp.tool

# ---------------------------
# SQLite Setup / Utilities
# ---------------------------

_lock = threading.Lock()

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with _lock, get_conn() as conn:
        cur = conn.cursor()
        cur.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS rooms (
                room_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                room_number TEXT UNIQUE NOT NULL,
                capacity    INTEGER NOT NULL,
                status      TEXT NOT NULL DEFAULT 'available' -- available | occupied
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                age         INTEGER NOT NULL,
                num_guests  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reservations (
                reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id        INTEGER NOT NULL,
                user_id        INTEGER NOT NULL,
                check_in       TEXT NOT NULL,  -- YYYY-MM-DD
                check_out      TEXT NOT NULL,  -- YYYY-MM-DD
                created_at     TEXT NOT NULL DEFAULT (datetime('now')),
                status         TEXT NOT NULL DEFAULT 'active', -- active | canceled | checked_out
                FOREIGN KEY (room_id) REFERENCES rooms(room_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            """
        )
        # Seed 10 rooms if empty
        cur.execute("SELECT COUNT(*) AS c FROM rooms;")
        if cur.fetchone()["c"] == 0:
            rooms = [
                ("101", 2), ("102", 2), ("103", 3), ("104", 2), ("105", 4),
                ("201", 2), ("202", 2), ("203", 3), ("204", 2), ("205", 4),
            ]
            cur.executemany("INSERT INTO rooms (room_number, capacity, status) VALUES (?, ?, 'available');", rooms)
        conn.commit()

def fetchone_dict(cur) -> Optional[Dict[str, Any]]:
    row = cur.fetchone()
    return dict(row) if row else None

def fetchall_dicts(cur) -> List[Dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]

def date_order_ok(check_in: str, check_out: str) -> bool:
    # lexical compare works for YYYY-MM-DD
    return check_in < check_out

def room_by_number(cur, room_number: str) -> Optional[Dict[str, Any]]:
    cur.execute("SELECT * FROM rooms WHERE room_number = ?;", (room_number,))
    r = cur.fetchone()
    return dict(r) if r else None

def user_get_or_create(cur, name: str, age: int, num_guests: int) -> int:
    # Create new user each booking OR deduplicate by (name, age, num_guests)
    cur.execute(
        "SELECT user_id FROM users WHERE name = ? AND age = ? AND num_guests = ? LIMIT 1;",
        (name, age, num_guests),
    )
    r = cur.fetchone()
    if r:
        return r["user_id"]
    cur.execute(
        "INSERT INTO users (name, age, num_guests) VALUES (?, ?, ?);",
        (name, age, num_guests),
    )
    return cur.lastrowid

def reservation_overlap_exists(cur, room_id: int, check_in: str, check_out: str) -> bool:
    # Overlap: NOT (existing_out <= new_in OR existing_in >= new_out)
    cur.execute(
        """
        SELECT reservation_id FROM reservations
        WHERE room_id = ?
          AND status = 'active'
          AND NOT (check_out <= ? OR check_in >= ?)
        LIMIT 1;
        """,
        (room_id, check_in, check_out),
    )
    return cur.fetchone() is not None

def update_room_status(cur, room_id: int) -> None:
    # A room is 'occupied' if any active reservation spans "today" OR at least one active reservation exists.
    # For simplicity (no "today" input), we mark 'occupied' if any active reservation exists.
    cur.execute(
        "SELECT COUNT(*) AS c FROM reservations WHERE room_id = ? AND status = 'active';",
        (room_id,),
    )
    occupied = (cur.fetchone()["c"] > 0)
    cur.execute(
        "UPDATE rooms SET status = ? WHERE room_id = ?;",
        ('occupied' if occupied else 'available', room_id)
    )

# ---------------------------
# MCP Tools
# ---------------------------

@_tool()
def list_rooms(available_only: bool = True) -> List[Dict[str, Any]]:
    """
    List rooms. If available_only is True, only rooms with status 'available' are returned.
    """
    with _lock, get_conn() as conn:
        cur = conn.cursor()
        if available_only:
            cur.execute("SELECT * FROM rooms WHERE status = 'available' ORDER BY room_number;")
        else:
            cur.execute("SELECT * FROM rooms ORDER BY room_number;")
        return fetchall_dicts(cur)

@_tool()
def create_user(name: str, age: int, num_guests: int) -> Dict[str, Any]:
    """
    Create a user (booker). Returns the created user record.
    """
    if age <= 0:
        raise ValueError("age must be positive")
    if num_guests <= 0:
        raise ValueError("num_guests must be positive")

    with _lock, get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (name, age, num_guests) VALUES (?, ?, ?);",
            (name, age, num_guests),
        )
        user_id = cur.lastrowid
        conn.commit()
        cur.execute("SELECT * FROM users WHERE user_id = ?;", (user_id,))
        return fetchone_dict(cur)  # type: ignore[return-value]

@_tool()
def book_room(
    room_number: str,
    name: str,
    age: int,
    num_guests: int,
    check_in: str,   # YYYY-MM-DD
    check_out: str,  # YYYY-MM-DD
) -> Dict[str, Any]:
    """
    Create a reservation if the room is available for the given period.
    - Validates date order, capacity, and overlapping bookings.
    - Auto-creates (or reuses) a user entry based on (name, age, num_guests).
    """
    if not date_order_ok(check_in, check_out):
        raise ValueError("check_in must be before check_out (YYYY-MM-DD)")

    if age <= 0:
        raise ValueError("age must be positive")
    if num_guests <= 0:
        raise ValueError("num_guests must be positive")

    with _lock, get_conn() as conn:
        cur = conn.cursor()
        r = room_by_number(cur, room_number)
        if not r:
            raise ValueError(f"Room {room_number} does not exist.")

        if num_guests > r["capacity"]:
            raise ValueError(f"Room {room_number} capacity is {r['capacity']}, got num_guests={num_guests}.")

        # Check overlap
        if reservation_overlap_exists(cur, r["room_id"], check_in, check_out):
            raise ValueError(f"Room {room_number} already reserved for the selected dates.")

        # Create or reuse user
        user_id = user_get_or_create(cur, name, age, num_guests)

        # Insert reservation
        cur.execute(
            """
            INSERT INTO reservations (room_id, user_id, check_in, check_out, status)
            VALUES (?, ?, ?, ?, 'active');
            """,
            (r["room_id"], user_id, check_in, check_out),
        )
        reservation_id = cur.lastrowid

        # Update room status
        update_room_status(cur, r["room_id"])
        conn.commit()

        # Return reservation info
        cur.execute(
            """
            SELECT res.*, rm.room_number, u.name AS user_name, u.age AS user_age, u.num_guests AS user_num_guests
            FROM reservations res
            JOIN rooms rm ON rm.room_id = res.room_id
            JOIN users u  ON u.user_id = res.user_id
            WHERE res.reservation_id = ?;
            """,
            (reservation_id,),
        )
        return fetchone_dict(cur)  # type: ignore[return-value]

@_tool()
def get_reservations(room_number: Optional[str] = None, name: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    List reservations, optionally filtered by room_number and/or user name.
    """
    with _lock, get_conn() as conn:
        cur = conn.cursor()
        base = """
        SELECT res.*, rm.room_number, u.name AS user_name, u.age AS user_age, u.num_guests AS user_num_guests
        FROM reservations res
        JOIN rooms rm ON rm.room_id = res.room_id
        JOIN users u  ON u.user_id = res.user_id
        """
        conds, params = [], []
        if room_number:
            conds.append("rm.room_number = ?")
            params.append(room_number)
        if name:
            conds.append("u.name = ?")
            params.append(name)
        if conds:
            base += " WHERE " + " AND ".join(conds)
        base += " ORDER BY res.created_at DESC;"
        cur.execute(base, tuple(params))
        return fetchall_dicts(cur)

@_tool()
def cancel_reservation(reservation_id: int) -> Dict[str, Any]:
    """
    Cancel an active reservation by ID.
    """
    with _lock, get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM reservations WHERE reservation_id = ?;", (reservation_id,))
        res = cur.fetchone()
        if not res:
            raise ValueError(f"Reservation {reservation_id} not found.")
        if res["status"] != "active":
            raise ValueError(f"Reservation {reservation_id} is not active (status={res['status']}).")

        cur.execute("UPDATE reservations SET status = 'canceled' WHERE reservation_id = ?;", (reservation_id,))
        update_room_status(cur, res["room_id"])
        conn.commit()

        cur.execute(
            """
            SELECT res.*, rm.room_number, u.name AS user_name
            FROM reservations res
            JOIN rooms rm ON rm.room_id = res.room_id
            JOIN users u  ON u.user_id = res.user_id
            WHERE reservation_id = ?;
            """,
            (reservation_id,),
        )
        return fetchone_dict(cur)  # type: ignore[return-value]

@_tool()
def checkout(reservation_id: int) -> Dict[str, Any]:
    """
    Mark a reservation as 'checked_out' and update room status.
    """
    with _lock, get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM reservations WHERE reservation_id = ?;", (reservation_id,))
        res = cur.fetchone()
        if not res:
            raise ValueError(f"Reservation {reservation_id} not found.")
        if res["status"] != "active":
            raise ValueError(f"Reservation {reservation_id} is not active (status={res['status']}).")

        cur.execute("UPDATE reservations SET status = 'checked_out' WHERE reservation_id = ?;", (reservation_id,))
        update_room_status(cur, res["room_id"])
        conn.commit()

        cur.execute(
            """
            SELECT res.*, rm.room_number, u.name AS user_name
            FROM reservations res
            JOIN rooms rm ON rm.room_id = res.room_id
            JOIN users u  ON u.user_id = res.user_id
            WHERE reservation_id = ?;
            """,
            (reservation_id,),
        )
        return fetchone_dict(cur)  # type: ignore[return-value]

# ---------------------------
# Main
# ---------------------------

if __name__ == "__main__":
    init_db()
    # Starts the MCP stdio server (works with MCP-compatible clients)
    mcp.run()
