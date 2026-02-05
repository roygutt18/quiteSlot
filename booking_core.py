import datetime as dt

# ---------- Time helpers ----------

def day_key(d: dt.date) -> str:
    # Mon=0 . Sun=6
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][d.weekday()]

def parse_hhmm(s: str) -> dt.time:
    return dt.datetime.strptime(s, "%H:%M").time()

def ceil_to_slot(dt_obj: dt.datetime, duration: int) -> dt.datetime:
    minutes = dt_obj.minute
    remainder = minutes % duration

    if remainder == 0:
        return dt_obj.replace(second=0, microsecond=0)

    delta = duration - remainder
    return (dt_obj + dt.timedelta(minutes=delta)).replace(second=0, microsecond=0)

# ---------- Working hours (supports default/by_day + legacy start/end) ----------

def get_working_hours_for_date(cfg: dict, date: dt.date) -> dict:
    """
    Returns:
    {
      "start": "HH:MM",
      "end": "HH:MM",
      "breaks": [ { "start": "HH:MM", "end": "HH:MM" }, ... ]
    }
    """

    wh = cfg.get("working_hours") or {}

    # legacy support
    if "start" in wh and "end" in wh:
        return {
            "start": wh["start"],
            "end": wh["end"],
            "breaks": []
        }

    default = wh.get("default") or {}
    by_day = wh.get("by_day") or {}

    dk = day_key(date)
    specific = by_day.get(dk) or {}

    start = specific.get("start") or default.get("start") or "09:00"
    end = specific.get("end") or default.get("end") or "17:00"

    breaks = specific.get("breaks")
    if breaks is None:
        breaks = default.get("breaks", [])

    return {
        "start": start,
        "end": end,
        "breaks": breaks or []
    }

# ---------- Working time checks ----------

def is_working_day(cfg: dict, date: dt.date):
    if day_key(date) not in cfg["working_days"]:
        return False, "העסק סגור ביום זה."
    return True, None

def is_closed_date(cfg: dict, date: dt.date):
    if date.isoformat() in set(cfg.get("closed_dates", [])):
        return False, "העסק סגור בתאריך זה."
    return True, None

def is_working_hours(cfg: dict, start: dt.datetime, end: dt.datetime):
    wh = get_working_hours_for_date(cfg, start.date())
    start_allowed = parse_hhmm(wh["start"])
    end_allowed = parse_hhmm(wh["end"])

    breaks = wh.get("breaks", [])
    for b in breaks:
        b_start = parse_hhmm(b["start"])
        b_end = parse_hhmm(b["end"])
        if start.time() < b_end and end.time() > b_start:
            return False, "יש הפסקה בזמן הזה"

    s_time = start.time()
    e_time = end.time()

    if s_time < start_allowed or e_time > end_allowed:
        # Calculate last possible start time for this duration
        try:
            duration_delta = end - start
            last_possible_start = (dt.datetime.combine(start.date(), end_allowed) - duration_delta).time()
            last_slot_str = last_possible_start.strftime("%H:%M")
        except:
            last_slot_str = wh["end"]

        return False, f"שעות הפעילות הן {wh['start']}–{wh['end']}. תור אחרון יכול להתחיל ב-{last_slot_str}"

    return True, None

def validate_slot(cfg: dict, start_local: dt.datetime, end_local: dt.datetime):
    if start_local.date() != end_local.date():
        return False, "תור חייב להיות באותו יום."

    ok, msg = is_working_day(cfg, start_local.date())
    if not ok:
        return False, msg

    ok, msg = is_closed_date(cfg, start_local.date())
    if not ok:
        return False, msg

    ok, msg = is_working_hours(cfg, start_local, end_local)
    if not ok:
        return False, msg

    return True, None
