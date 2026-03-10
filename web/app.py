import os
from datetime import datetime, timezone

import asyncpg
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

app = FastAPI()
templates = Jinja2Templates(directory="templates")

SECRET_KEY = os.environ["SECRET_KEY"]
DASHBOARD_PASSWORD = os.environ["DASHBOARD_PASSWORD"]
DATABASE_URL = os.environ["DATABASE_URL"]
SESSION_COOKIE = "session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days

signer = URLSafeTimedSerializer(SECRET_KEY)
db_pool: asyncpg.Pool | None = None


@app.on_event("startup")
async def startup():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)


def make_session_cookie() -> str:
    return signer.dumps("authenticated")


def check_session(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    try:
        signer.loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if check_session(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request, password: str = Form(...)):
    if password == DASHBOARD_PASSWORD:
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(SESSION_COOKIE, make_session_cookie(), max_age=SESSION_MAX_AGE, httponly=True)
        return response
    return templates.TemplateResponse("login.html", {"request": request, "error": "Incorrect password"})


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not check_session(request):
        return RedirectResponse("/login", status_code=302)

    async with db_pool.acquire() as conn:
        guilds = await conn.fetch(
            "SELECT DISTINCT guild_id, guild_name FROM ticket_events ORDER BY guild_name"
        )
        categories = await conn.fetch(
            "SELECT DISTINCT category_id, category_name, guild_id FROM ticket_events ORDER BY category_name"
        )

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "guilds": [dict(r) for r in guilds],
        "categories": [dict(r) for r in categories],
    })


@app.get("/api/events")
async def api_events(
    request: Request,
    guild_id: str = "",
    category_id: str = "",
    start: str = "",
    end: str = "",
    group_by: str = "day",
):
    if not check_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # Build dynamic WHERE clause
    conditions = ["event_type != 'snapshot'"]
    params = []

    if guild_id:
        params.append(guild_id)
        conditions.append(f"guild_id = ${len(params)}")
    if category_id:
        params.append(category_id)
        conditions.append(f"category_id = ${len(params)}")
    if start:
        params.append(datetime.fromisoformat(start).replace(tzinfo=timezone.utc))
        conditions.append(f"timestamp >= ${len(params)}")
    if end:
        # Include the full end day by moving to start of next day
        end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
        params.append(end_dt)
        conditions.append(f"timestamp <= ${len(params)}")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    trunc = "hour" if group_by == "hour" else "day"

    async with db_pool.acquire() as conn:
        # Volume over time
        volume_rows = await conn.fetch(f"""
            SELECT
                DATE_TRUNC('{trunc}', timestamp) AS period,
                event_type,
                COUNT(*) AS count
            FROM ticket_events
            {where}
            GROUP BY period, event_type
            ORDER BY period
        """, *params)

        # Volume by hour of day (aggregated)
        hourly_rows = await conn.fetch(f"""
            SELECT
                EXTRACT(HOUR FROM timestamp)::int AS hour,
                event_type,
                COUNT(*) AS count
            FROM ticket_events
            {where}
            GROUP BY hour, event_type
            ORDER BY hour
        """, *params)

        # Volume by category
        category_rows = await conn.fetch(f"""
            SELECT
                category_name,
                event_type,
                COUNT(*) AS count
            FROM ticket_events
            {where}
            GROUP BY category_name, event_type
            ORDER BY category_name
        """, *params)

        # Capacity hits by category (open events where channel_count >= 50)
        cap_conditions = ["event_type = 'open'", "channel_count >= 50"]
        cap_params = []
        if guild_id:
            cap_params.append(guild_id)
            cap_conditions.append(f"guild_id = ${len(cap_params)}")
        if category_id:
            cap_params.append(category_id)
            cap_conditions.append(f"category_id = ${len(cap_params)}")
        if start:
            cap_params.append(datetime.fromisoformat(start).replace(tzinfo=timezone.utc))
            cap_conditions.append(f"timestamp >= ${len(cap_params)}")
        if end:
            cap_end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
            cap_end_dt = cap_end_dt.replace(hour=23, minute=59, second=59)
            cap_params.append(cap_end_dt)
            cap_conditions.append(f"timestamp <= ${len(cap_params)}")
        cap_where = "WHERE " + " AND ".join(cap_conditions)
        capacity_rows = await conn.fetch(f"""
            SELECT category_name, COUNT(*) AS count
            FROM ticket_events
            {cap_where}
            GROUP BY category_name
            ORDER BY count DESC
        """, *cap_params)

    def serialize(rows):
        result = []
        for r in rows:
            row = dict(r)
            if "period" in row and row["period"]:
                row["period"] = row["period"].isoformat()
            result.append(row)
        return result

    return JSONResponse({
        "volume": serialize(volume_rows),
        "hourly": serialize(hourly_rows),
        "by_category": serialize(category_rows),
        "capacity": serialize(capacity_rows),
    })


@app.get("/api/live")
async def api_live(request: Request):
    if not check_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (guild_id, category_id)
                guild_id, guild_name, category_id, category_name,
                channel_count, event_type, timestamp, oldest_channel_ts
            FROM ticket_events
            ORDER BY guild_id, category_id, timestamp DESC
        """)

        # For at-capacity categories, find the last time they were NOT full
        # (approximates when the current full-streak started)
        not_full_rows = await conn.fetch("""
            SELECT DISTINCT ON (guild_id, category_id)
                guild_id, category_id, timestamp
            FROM ticket_events
            WHERE channel_count < 50
              AND (guild_id, category_id) IN (
                  SELECT guild_id, category_id FROM (
                      SELECT DISTINCT ON (guild_id, category_id)
                          guild_id, category_id, channel_count
                      FROM ticket_events
                      ORDER BY guild_id, category_id, timestamp DESC
                  ) sub
                  WHERE channel_count >= 50
              )
            ORDER BY guild_id, category_id, timestamp DESC
        """)
        last_not_full = {(r["guild_id"], r["category_id"]): r["timestamp"] for r in not_full_rows}

        # Fallback: earliest event for at-capacity categories that have no "not full" record
        earliest_rows = await conn.fetch("""
            SELECT DISTINCT ON (guild_id, category_id)
                guild_id, category_id, timestamp
            FROM ticket_events
            WHERE (guild_id, category_id) IN (
                SELECT guild_id, category_id FROM (
                    SELECT DISTINCT ON (guild_id, category_id)
                        guild_id, category_id, channel_count
                    FROM ticket_events
                    ORDER BY guild_id, category_id, timestamp DESC
                ) sub
                WHERE channel_count >= 50
            )
            ORDER BY guild_id, category_id, timestamp ASC
        """)
        earliest = {(r["guild_id"], r["category_id"]): r["timestamp"] for r in earliest_rows}

    result = []
    for r in rows:
        key = (r["guild_id"], r["category_id"])
        if r["channel_count"] >= 50:
            since_ts = last_not_full.get(key) or earliest.get(key)
            capacity_since = since_ts.isoformat() if since_ts else None
        else:
            capacity_since = None
        oldest_ts = r["oldest_channel_ts"]
        result.append({
            "guild_id": r["guild_id"],
            "guild_name": r["guild_name"],
            "category_id": r["category_id"],
            "category_name": r["category_name"],
            "channel_count": r["channel_count"],
            "event_type": r["event_type"],
            "timestamp": r["timestamp"].isoformat(),
            "capacity_since": capacity_since,
            "oldest_channel_ts": oldest_ts.isoformat() if oldest_ts else None,
        })

    return JSONResponse(result)
