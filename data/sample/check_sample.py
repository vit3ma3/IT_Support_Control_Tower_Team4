"""Эталонный пересчёт контрольного набора data/sample.

Считает ожидаемые значения для tests/README.md по правилам из data/sample/README.md:
проверки негативного набора, реакция и решение, нарушения SLA, загрузка, фильтры, экспорт.
Запуск: python3 data/sample/check_sample.py
"""
import csv
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
FMT = "%Y-%m-%d %H:%M:%S"
QUEUES = {"L1", "L2"}
ASSIGNEES = {"E01", "E02", "E03"}
PERIOD = ("2026-11-02", "2026-11-03")


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def ts(value):
    return datetime.strptime(value, FMT) if value else None


def read_set(folder):
    tickets = read(folder / "tickets.csv")
    events = defaultdict(list)
    for e in read(folder / "events.csv"):
        events[e["ticket_id"]].append(e)
    return tickets, events


def validate(tickets, events, targets):
    """Возвращает (принятые обращения, список ошибок). Обращение с ошибкой отклоняется целиком."""
    accepted, errors = [], []
    for t in tickets:
        evs = events[t["ticket_id"]]
        found = []
        if not all(t[k] for k in ("ticket_id", "created_at", "priority", "queue", "assignee")):
            found.append("V2 обязательные поля")
        if t["queue"] not in QUEUES or t["assignee"] not in ASSIGNEES:
            found.append("V3 ссылки")
        kinds = [e["event_type"] for e in evs]
        if kinds.count("first_response") > 1 or kinds.count("resolved") > 1:
            found.append("V5 уникальность событий")
        if t["priority"] and t["priority"] not in targets:
            found.append("V6 норматив SLA")
        if t["created_at"]:
            created = ts(t["created_at"])
            first = {e["event_type"]: ts(e["ts"]) for e in evs}
            chain = [created] + [first[k] for k in ("first_response", "resolved") if k in first]
            if any(a > b for a, b in zip(chain, chain[1:])):
                found.append("V4 хронология событий")
            if t["closed_at"] and created > ts(t["closed_at"]):
                found.append("V4 created_at > closed_at")
        if found:
            errors.append((t["ticket_id"], found))
        else:
            accepted.append(t)
    return accepted, errors


def status(minutes, created, norm, period_end):
    """Нарушение: факт > норматива. Нет события: срок истёк к концу периода — нарушение, иначе не определено."""
    if minutes is not None:
        return minutes > norm
    return True if created + timedelta(minutes=norm) < period_end else None


def metrics(tickets, events, targets, period=PERIOD):
    period_end = datetime.strptime(period[1], "%Y-%m-%d") + timedelta(days=1)
    rows = []
    for t in tickets:
        ev = {e["event_type"]: ts(e["ts"]) for e in events[t["ticket_id"]]}
        created = ts(t["created_at"])
        resp = (ev["first_response"] - created).total_seconds() / 60 if "first_response" in ev else None
        res = (ev["resolved"] - created).total_seconds() / 60 if "resolved" in ev else None
        resp_t, res_t = targets[t["priority"]]
        rows.append({
            **t,
            "assigned": ev.get("assigned"),
            "resolved": ev.get("resolved"),
            "resp": resp,
            "res": res,
            "resp_breach": status(resp, created, resp_t, period_end),
            "res_breach": status(res, created, res_t, period_end),
        })
    return rows


def days(period):
    d, end = datetime.strptime(period[0], "%Y-%m-%d"), datetime.strptime(period[1], "%Y-%m-%d")
    while d <= end:
        yield d
        d += timedelta(days=1)


def daily_load(rows, period):
    """{(исполнитель, день): число активных обращений}; интервал assigned → resolved, открытое — до конца периода."""
    end_of_period = datetime.strptime(period[1], "%Y-%m-%d") + timedelta(days=1)
    load = defaultdict(int)
    for r in rows:
        start, finish = r["assigned"], r["resolved"] or end_of_period
        for d in days(period):
            if start < d + timedelta(days=1) and finish >= d:
                load[(r["assignee"], d.strftime("%d.%m"))] += 1
    return dict(load)


def avg(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 2) if values else None


def rate(flags):
    flags = [f for f in flags if f is not None]
    return round(100 * sum(flags) / len(flags), 1) if flags else None


def summary(rows):
    # breach_rate: обращения с хотя бы одним нарушением / обращения, где хотя бы одно нарушение или оба показателя определены
    decided = [r for r in rows if r["resp_breach"] or r["res_breach"]
               or (r["resp_breach"] is not None and r["res_breach"] is not None)]
    return {
        "обращений": len(rows),
        "resp_avg": avg(r["resp"] for r in rows),
        "resolve_avg": avg(r["res"] for r in rows),
        "breach_resp, %": rate(r["resp_breach"] for r in rows),
        "breach_resolve, %": rate(r["res_breach"] for r in rows),
        "breach_rate, %": rate(bool(r["resp_breach"] or r["res_breach"]) for r in decided),
    }


def in_period(r, period):
    """SLA считается по обращениям, созданным в периоде; загрузка — по всем обращениям, активным в дни периода."""
    return period[0] <= r["created_at"][:10] <= period[1]


def export(rows, period):
    out = []
    for key in sorted({(r["queue"], r["priority"]) for r in rows}):
        group = [r for r in rows if (r["queue"], r["priority"]) == key]
        load = daily_load(group, period)
        s = summary(group)
        out.append([*key, s["resp_avg"], s["resolve_avg"], s["breach_rate, %"], avg(load.values())])
    return out


def main():
    targets = {r["priority"]: (int(r["response_min"]), int(r["resolve_min"])) for r in read(HERE / "sla_targets.csv")}

    print("== Негативный набор")
    with open(HERE / "negative/tickets_missing_column.csv", encoding="utf-8") as f:
        header = f.readline().strip().split(",")
    missing = {"ticket_id", "created_at", "priority", "queue", "assignee", "closed_at"} - set(header)
    print("V1 структура, нет колонок:", sorted(missing))
    accepted, errors = validate(*read_set(HERE / "negative"), targets)
    for ticket_id, found in errors:
        print(ticket_id, found)
    print("принято:", [t["ticket_id"] for t in accepted], "отклонено:", len(errors))

    tickets, events = read_set(HERE)
    accepted, errors = validate(tickets, events, targets)
    print("\n== Контрольный набор: принято", len(accepted), "обращений,",
          sum(len(events[t["ticket_id"]]) for t in accepted), "событий, ошибок", len(errors))
    rows = metrics(accepted, events, targets)
    for r in rows:
        print(r["ticket_id"], r["priority"], r["queue"], r["assignee"], "resp", r["resp"], r["resp_breach"],
              "res", r["res"], r["res_breach"])
    print("\nИтого:", summary(rows))

    load = daily_load(rows, PERIOD)
    print("\nЗагрузка по дням:", sorted(load.items()), "средняя:", avg(load.values()),
          "разброс: макс", max(load.values()), "мин", min(load.values()))

    print("\n== Фильтры")
    for name, cond, period in [
        ("период 03.11.26", lambda r: True, ("2026-11-03", "2026-11-03")),
        ("очередь L2", lambda r: r["queue"] == "L2", PERIOD),
        ("исполнитель E03", lambda r: r["assignee"] == "E03", PERIOD),
        ("приоритет P1", lambda r: r["priority"] == "P1", PERIOD),
    ]:
        sel = [r for r in rows if cond(r) and in_period(r, period)]
        active = [r for r in rows if cond(r)]
        print(name, [r["ticket_id"] for r in sel], summary(sel), "загрузка", avg(daily_load(active, period).values()))

    print("\n== Экспорт: queue,priority,resp_avg,resolve_avg,breach_rate,load_avg")
    for line in export(rows, PERIOD):
        print(",".join("" if v is None else str(v) for v in line))

    targets["P1"] = (20, 60)
    print("\n== Норматив реакции P1 изменён на 20 мин:", summary(metrics(accepted, events, targets)))


if __name__ == "__main__":
    main()
