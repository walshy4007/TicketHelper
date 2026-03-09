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
    conditions = []
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
        params.append(datetime.fromisoformat(end).replace(tzinfo=timezone.utc))
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
    })
