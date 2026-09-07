#!/usr/bin/env python3
"""Export inactive Yandex 360 users with their current departments.

Only the Python standard library is required. Statistics are processed page by
page. Per-user date sets are retained to check coverage and duplicate days.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from collections import Counter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


STATISTICS_URL = (
    "https://cloud-api.yandex.net/v1/directory/organizations/"
    "{org_id}/users/statistics"
)
USERS_URL = "https://api360.yandex.net/directory/v1/org/{org_id}/users"
DEPARTMENTS_URL = "https://api360.yandex.net/directory/v1/org/{org_id}/departments"

USER_PAGE_SIZE = 1000
DEPARTMENT_PAGE_SIZE = 100
SERVICE_PREFIXES = ("mail_", "disk_", "messenger_", "telemost_")
LAST_USAGE_FIELDS = (
    "mail_last_usage_date",
    "disk_last_usage_date",
    "messenger_last_usage_date",
    "telemost_last_usage_date",
)
IDENTITY_FIELDS = (
    "user_id",
    "nickname",
    "account_type",
    "name",
    "is_admin",
    "is_manager",
    "is_enabled",
)
OUTPUT_FIELDS = (
    "user_id",
    "nickname",
    "account_type",
    "name",
    "department_id",
    "department_name",
    "department_lookup_status",
    "is_admin",
    "is_manager",
    "is_enabled",
    "report_start_date",
    "report_end_date",
    "statistics_rows_checked",
    "created_at", "cutoff_date", "last_activity_date",
    "classification", "classification_reason", "inactive_candidate",
    "statistics_status", "statistics_days_checked", "report_timezone",
    "directory_is_enabled", "is_robot", "is_dismissed",
    "mail_received_in_period",
    *LAST_USAGE_FIELDS,
)


class ScriptError(Exception):
    """Expected error that should be displayed without a traceback."""


@dataclass
class UserStatisticsState:
    identity: dict[str, Any] = field(default_factory=dict)
    latest_row_date: str = ""
    rows_checked: int = 0
    has_activity: bool = False
    row_dates: set[date] = field(default_factory=set)
    issues: set[str] = field(default_factory=set)
    mail_received: bool = False
    last_usage_dates: dict[str, str | None] = field(
        default_factory=lambda: {name: None for name in LAST_USAGE_FIELDS}
    )


@dataclass
class DepartmentLookupCounters:
    user_not_found: int = 0
    department_not_set: int = 0
    department_not_found: int = 0
    department_name_empty: int = 0

    @property
    def problems_total(self) -> int:
        return (
            self.user_not_found
            + self.department_not_set
            + self.department_not_found
            + self.department_name_empty
        )


def parse_iso_date(value: str, argument_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ScriptError(
            f"{argument_name}: ожидается дата в формате YYYY-MM-DD, получено: {value}"
        ) from exc


def validate_period(start_date: date, end_date: date) -> None:
    if start_date > end_date:
        raise ScriptError("Начальная дата не может быть позже конечной даты.")


def normalize_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    normalized = str(value).strip()
    return normalized or None


def request_json(
    url: str,
    token: str,
    timeout: int,
    max_retries: int = 4,
) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Authorization": f"OAuth {token}",
            "Accept": "application/json",
            "User-Agent": "y360-inactive-users-departments-export/2.0",
        },
        method="GET",
    )

    for attempt in range(max_retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ScriptError("API вернул JSON неожиданного формата.")
            return payload
        except HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            if retryable and attempt < max_retries:
                retry_after = exc.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 2**attempt
                except ValueError:
                    delay = 2**attempt
                time.sleep(min(delay, 30))
                continue

            detail = response_body.strip()
            if detail:
                try:
                    detail = json.dumps(json.loads(detail), ensure_ascii=False)
                except json.JSONDecodeError:
                    pass
            raise ScriptError(
                f"Ошибка API: HTTP {exc.code}. {detail or exc.reason}"
            ) from exc
        except URLError as exc:
            if attempt < max_retries:
                time.sleep(2**attempt)
                continue
            raise ScriptError(f"Не удалось подключиться к API: {exc.reason}") from exc
        except TimeoutError as exc:
            if attempt < max_retries:
                time.sleep(2**attempt)
                continue
            raise ScriptError("Истекло время ожидания ответа API.") from exc
        except json.JSONDecodeError as exc:
            raise ScriptError("API вернул ответ, который не является корректным JSON.") from exc

    raise ScriptError("Не удалось получить ответ API после повторных попыток.")


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def fetch_directory_collection(
    *,
    url: str,
    collection_name: str,
    token: str,
    timeout: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], int]:
    entities: list[dict[str, Any]] = []
    page = 1
    pages_loaded = 0

    while True:
        payload = request_json(
            f"{url}?{urlencode({'page': page, 'perPage': page_size})}",
            token,
            timeout,
        )
        pages_loaded += 1
        page_entities = payload.get(collection_name)
        if not isinstance(page_entities, list):
            raise ScriptError(
                f"В ответе API отсутствует массив {collection_name} на странице {page}."
            )
        if any(not isinstance(entity, dict) for entity in page_entities):
            raise ScriptError(
                f"Массив {collection_name} содержит элемент неожиданного формата."
            )
        entities.extend(page_entities)

        response_page = positive_int(payload.get("page")) or page
        response_pages = positive_int(payload.get("pages"))
        response_total = positive_int(payload.get("total"))
        response_per_page = positive_int(payload.get("perPage")) or page_size

        if response_pages is not None:
            if response_page >= response_pages:
                break
        elif response_total is not None:
            if len(entities) >= response_total:
                break
        elif not page_entities or len(page_entities) < response_per_page:
            break

        if pages_loaded > 100000:
            raise ScriptError(
                f"Слишком много страниц {collection_name}; загрузка остановлена."
            )
        page = response_page + 1

    return entities, pages_loaded


def fetch_departments(
    token: str, org_id: str, timeout: int
) -> tuple[dict[str, str], int]:
    departments, pages = fetch_directory_collection(
        url=DEPARTMENTS_URL.format(org_id=org_id),
        collection_name="departments",
        token=token,
        timeout=timeout,
        page_size=DEPARTMENT_PAGE_SIZE,
    )
    result: dict[str, str] = {}
    for department in departments:
        department_id = normalize_id(department.get("id"))
        if department_id is None:
            raise ScriptError("В справочнике найдено подразделение без поля id.")
        name = department.get("name")
        result[department_id] = name.strip() if isinstance(name, str) else ""
    return result, pages


def fetch_users(
    token: str, org_id: str, timeout: int
) -> tuple[dict[str, dict[str, Any]], int]:
    users, pages = fetch_directory_collection(
        url=USERS_URL.format(org_id=org_id),
        collection_name="users",
        token=token,
        timeout=timeout,
        page_size=USER_PAGE_SIZE,
    )
    result: dict[str, dict[str, Any]] = {}
    for user in users:
        user_id = normalize_id(user.get("id"))
        if user_id is None:
            raise ScriptError("В справочнике найден пользователь без поля id.")
        result[user_id] = user
    return result, pages


def value_is_nonzero(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return False
        try:
            return float(stripped) != 0
        except ValueError:
            return True
    return bool(value)


def update_statistics_state(
    states: dict[str, UserStatisticsState],
    item: dict[str, Any],
    start_date: date,
    end_date: date,
) -> None:
    user_id = normalize_id(item.get("user_id"))
    if user_id is None:
        raise ScriptError("В строке статистики отсутствует user_id.")
    state = states.setdefault(user_id, UserStatisticsState())
    state.rows_checked += 1
    try:
        row_date = date.fromisoformat(item["date"])
        if not start_date <= row_date <= end_date:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        state.issues.add("invalid_row_date")
        return
    if row_date in state.row_dates:
        state.issues.add("duplicate_row_date")
    state.row_dates.add(row_date)
    if not state.identity or row_date.isoformat() >= state.latest_row_date:
        state.identity = {name: item.get(name) for name in IDENTITY_FIELDS}
        state.latest_row_date = row_date.isoformat()
    state.mail_received |= value_is_nonzero(item.get("mail_received_letters_count"))
    for name in LAST_USAGE_FIELDS:
        if name not in item:
            state.issues.add("missing_usage_field:" + name)
            continue
        value = item[name]
        if value is None or value == "":
            continue
        try:
            usage = date.fromisoformat(value)
        except (TypeError, ValueError):
            state.issues.add("invalid_usage_date:" + name)
            continue
        if usage > end_date:
            state.issues.add("usage_after_report_end:" + name)
            continue
        previous = state.last_usage_dates[name]
        if previous is None or usage.isoformat() > previous:
            state.last_usage_dates[name] = usage.isoformat()
        if usage >= start_date:
            state.has_activity = True


def stream_statistics_chunk(
    *,
    token: str,
    org_id: str,
    start_date: date,
    end_date: date,
    limit: int,
    timeout: int,
    states: dict[str, UserStatisticsState],
    report_start: date,
    report_end: date,
) -> tuple[dict[str, UserStatisticsState], int, int]:
    base_url = STATISTICS_URL.format(org_id=org_id)
    iteration_key = ""
    seen_iteration_keys: set[str] = set()
    rows_total = 0
    pages_total = 0

    while True:
        params = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "limit": str(limit),
        }
        if iteration_key:
            params["iteration_key"] = iteration_key

        payload = request_json(f"{base_url}?{urlencode(params)}", token, timeout)
        pages_total += 1
        items = payload.get("items")
        if not isinstance(items, list):
            raise ScriptError(
                f"В ответе статистики на странице {pages_total} отсутствует массив items."
            )

        for item in items:
            if not isinstance(item, dict):
                raise ScriptError(
                    f"Страница статистики {pages_total} содержит элемент "
                    "неожиданного формата."
                )
            update_statistics_state(states, item, report_start, report_end)
            rows_total += 1

        next_key = payload.get("iteration_key") or ""
        if not isinstance(next_key, str):
            raise ScriptError("API вернул iteration_key неожиданного формата.")
        if not next_key:
            break
        if next_key in seen_iteration_keys:
            raise ScriptError("API повторно вернул тот же iteration_key; загрузка остановлена.")
        seen_iteration_keys.add(next_key)
        iteration_key = next_key

    return states, rows_total, pages_total


def stream_statistics(*, token, org_id, start_date, end_date, limit, timeout):
    states = {}
    rows = pages = 0
    chunk_start = start_date
    while chunk_start <= end_date:
        chunk_end = min(chunk_start + timedelta(days=27), end_date)
        part, nr, np = stream_statistics_chunk(
            token=token, org_id=org_id, start_date=chunk_start,
            end_date=chunk_end, limit=limit, timeout=timeout,
            states=states, report_start=start_date, report_end=end_date,
        )
        rows += nr
        pages += np
        chunk_start = chunk_end + timedelta(days=1)
    return states, rows, pages


def resolve_department(
    *,
    user_id: str,
    user_departments: dict[str, str | None],
    departments: dict[str, str],
    counters: DepartmentLookupCounters,
) -> tuple[str | None, str, str]:
    if user_id not in user_departments:
        counters.user_not_found += 1
        return None, "", "user_not_found"

    department_id = user_departments[user_id]
    if department_id is None:
        counters.department_not_set += 1
        return None, "", "department_not_set"
    if department_id not in departments:
        counters.department_not_found += 1
        return department_id, "", "department_not_found"

    department_name = departments[department_id]
    if not department_name:
        counters.department_name_empty += 1
        return department_id, "", "department_name_empty"
    return department_id, department_name, "ok"


def classify(user, state, start_date, end_date, report_tz):
    if user is None:
        return "insufficient_data", "user_not_in_directory"
    if user.get("isRobot") is True:
        return "excluded", "service_account"
    if user.get("isDismissed") is True:
        return "excluded", "dismissed"
    if user.get("isEnabled") is False:
        return "excluded", "blocked"
    try:
        created = datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError()
        created_date = created.astimezone(report_tz).date()
    except (KeyError, AttributeError, TypeError, ValueError):
        return "insufficient_data", "invalid_or_missing_created_at"
    if created_date > end_date:
        return "excluded", "created_after_report_end"
    if created_date >= start_date:
        return "new_account", "created_on_or_after_cutoff"
    if state is None:
        return "insufficient_data", "no_statistics"
    if state.has_activity:
        return "active", "usage_in_period"
    if state.issues:
        return "insufficient_data", ";".join(sorted(state.issues))
    if len(state.row_dates) != (end_date - start_date).days + 1:
        return "insufficient_data", "incomplete_daily_coverage"
    if any(state.last_usage_dates.values()):
        return "inactive", "all_known_usage_before_cutoff"
    return "no_activity_recorded", "all_usage_dates_empty"


def build_inactive_rows(
    *, states, users, departments, start_date, end_date,
    report_tz=timezone.utc,
):
    rows = []
    counters = DepartmentLookupCounters()
    user_departments = {uid: normalize_id(u.get("departmentId")) for uid, u in users.items()}
    for user_id in sorted(set(users) | set(states)):
        state = states.get(user_id)
        user = users.get(user_id)
        status, reason = classify(user, state, start_date, end_date, report_tz)
        department_id, department_name, lookup_status = resolve_department(
            user_id=user_id, user_departments=user_departments,
            departments=departments, counters=counters,
        )
        row = dict(state.identity) if state else {}
        directory = user or {}
        row.setdefault("nickname", directory.get("nickname"))
        if not row.get("name"):
            name = directory.get("name") or {}
            row["name"] = " ".join(str(name.get(k) or "") for k in ("last", "first", "middle")).strip()
        dates = state.last_usage_dates if state else dict.fromkeys(LAST_USAGE_FIELDS)
        issues = sorted(state.issues) if state else []
        coverage_ok = bool(state and len(state.row_dates) == (end_date-start_date).days+1)
        row.update({
            "user_id": user_id, "department_id": department_id,
            "department_name": department_name, "department_lookup_status": lookup_status,
            "report_start_date": start_date.isoformat(), "report_end_date": end_date.isoformat(),
            "statistics_rows_checked": state.rows_checked if state else 0,
            "statistics_days_checked": len(state.row_dates) if state else 0,
            "statistics_status": "no_statistics" if state is None else (
                ";".join(issues) if issues else ("ok" if coverage_ok else "incomplete_daily_coverage")),
            "created_at": directory.get("createdAt"), "cutoff_date": start_date.isoformat(),
            "last_activity_date": max((v for v in dates.values() if v), default=None),
            "classification": status, "classification_reason": reason,
            "inactive_candidate": status in ("inactive", "no_activity_recorded"),
            "report_timezone": str(report_tz),
            "directory_is_enabled": directory.get("isEnabled"),
            "is_robot": directory.get("isRobot"), "is_dismissed": directory.get("isDismissed"),
            "mail_received_in_period": state.mail_received if state else None,
        })
        row.update(dates)
        rows.append(row)
    rows.sort(key=lambda r: (str(r["department_name"]).casefold(), str(r.get("name") or "").casefold(), r["user_id"]))
    return rows, counters


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.DictWriter(
            output_file,
            fieldnames=OUTPUT_FIELDS,
            delimiter=";",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as output_file:
        json.dump({"items": rows}, output_file, ensure_ascii=False, indent=2)
        output_file.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Экспорт пользователей без активности в Почте, Диске, Мессенджере "
            "и Телемосте с названиями текущих подразделений."
        )
    )
    parser.add_argument("start_date", help="Начало периода, YYYY-MM-DD")
    parser.add_argument("end_date", help="Конец периода, YYYY-MM-DD")
    parser.add_argument(
        "--format",
        choices=("csv", "json"),
        default="csv",
        help="Формат результата (по умолчанию: csv)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Путь к итоговому файлу; по умолчанию имя формируется автоматически",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Количество строк на одну страницу статистики (по умолчанию: 1000)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Тайм-аут одного запроса в секундах (по умолчанию: 60)",
    )
    parser.add_argument("--all-users", action="store_true", help="Выгрузить все статусы, включая недостаточные данные")
    parser.add_argument("--timezone", default="Europe/Moscow", help="Часовой пояс календарных границ (по умолчанию Europe/Moscow)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        token = os.environ.get("OAUTH_TOKEN", "").strip()
        org_id = os.environ.get("ORG_ID", "").strip()
        if not token:
            raise ScriptError("Не задана переменная окружения OAUTH_TOKEN.")
        if not org_id:
            raise ScriptError("Не задана переменная окружения ORG_ID.")
        if args.limit <= 0:
            raise ScriptError("Параметр --limit должен быть больше нуля.")
        if args.timeout <= 0:
            raise ScriptError("Параметр --timeout должен быть больше нуля.")

        start_date = parse_iso_date(args.start_date, "start_date")
        end_date = parse_iso_date(args.end_date, "end_date")
        try:
            report_tz = ZoneInfo(args.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ScriptError("Неизвестный часовой пояс; проверьте --timezone и наличие базы tzdata.") from exc
        validate_period(start_date, end_date)
        if end_date >= datetime.now(report_tz).date():
            raise ScriptError("Конец периода должен быть раньше сегодня в часовом поясе отчёта.")
        output_path = args.output or Path(
            f"{'all' if args.all_users else 'inactive'}_y360_users_{start_date}_{end_date}.{args.format}"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        print("Получение справочника подразделений...", file=sys.stderr)
        departments, department_pages = fetch_departments(token, org_id, args.timeout)

        print("Получение справочника пользователей...", file=sys.stderr)
        users, user_pages = fetch_users(token, org_id, args.timeout)

        print("Получение и обработка статистики...", file=sys.stderr)
        states, statistics_rows, statistics_pages = stream_statistics(
            token=token,
            org_id=org_id,
            start_date=start_date,
            end_date=end_date,
            limit=args.limit,
            timeout=args.timeout,
        )
        inactive_rows, lookup_counters = build_inactive_rows(
            states=states,
            users=users,
            departments=departments,
            start_date=start_date,
            end_date=end_date,
            report_tz=report_tz,
        )

        totals = Counter(row["classification"] for row in inactive_rows)
        print("Классификация: " + json.dumps(dict(totals), ensure_ascii=False))
        if not args.all_users:
            inactive_rows = [row for row in inactive_rows if row["inactive_candidate"]]
        if args.format == "csv":
            write_csv(output_path, inactive_rows)
        else:
            write_json(output_path, inactive_rows)

        print(f"Получено подразделений: {len(departments)} ({department_pages} стр.)")
        print(f"Получено пользователей справочника: {len(users)} ({user_pages} стр.)")
        print(f"Получено строк статистики: {statistics_rows} ({statistics_pages} стр.)")
        print(f"Проверено пользователей статистики: {len(states)}")
        print(f"Строк в выгрузке: {len(inactive_rows)}")
        print(
            "Не удалось определить подразделение: "
            f"{lookup_counters.problems_total} "
            f"(нет пользователя: {lookup_counters.user_not_found}; "
            f"не задан departmentId: {lookup_counters.department_not_set}; "
            f"нет подразделения: {lookup_counters.department_not_found}; "
            f"пустое название: {lookup_counters.department_name_empty})"
        )
        print(f"Результат: {output_path.resolve()}")
        return 0
    except ScriptError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Ошибка работы с файлом: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
