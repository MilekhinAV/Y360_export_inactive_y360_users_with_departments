#!/usr/bin/env python3
"""Export inactive Yandex 360 users with their current departments.

Only the Python standard library is required. Statistics are processed page by
page, so memory usage grows with the number of users, not with user-days.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date
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
    if end_date >= date.today():
        raise ScriptError(
            "Конечная дата должна быть раньше текущего дня: статистика за сегодня "
            "станет доступна только завтра."
        )


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


def fetch_user_departments(
    token: str, org_id: str, timeout: int
) -> tuple[dict[str, str | None], int]:
    users, pages = fetch_directory_collection(
        url=USERS_URL.format(org_id=org_id),
        collection_name="users",
        token=token,
        timeout=timeout,
        page_size=USER_PAGE_SIZE,
    )
    result: dict[str, str | None] = {}
    for user in users:
        user_id = normalize_id(user.get("id"))
        if user_id is None:
            raise ScriptError("В справочнике найден пользователь без поля id.")
        result[user_id] = normalize_id(user.get("departmentId"))
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


def usage_date_is_in_period(value: Any, start_date: date, end_date: date) -> bool:
    if value is None or value == "":
        return False
    if not isinstance(value, str):
        return True
    try:
        usage_date = date.fromisoformat(value)
    except ValueError:
        # Unknown non-empty values must not be silently treated as inactivity.
        return True
    return start_date <= usage_date <= end_date


def row_has_activity(item: dict[str, Any], start_date: date, end_date: date) -> bool:
    for field_name, value in item.items():
        if not field_name.startswith(SERVICE_PREFIXES):
            continue
        if field_name.endswith("_last_usage_date"):
            if usage_date_is_in_period(value, start_date, end_date):
                return True
        elif value_is_nonzero(value):
            return True
    return False


def update_statistics_state(
    states: dict[str, UserStatisticsState],
    item: dict[str, Any],
    start_date: date,
    end_date: date,
) -> None:
    user_id = normalize_id(item.get("user_id"))
    if user_id is None:
        raise ScriptError("В одной из строк статистики отсутствует user_id.")

    state = states.setdefault(user_id, UserStatisticsState())
    state.rows_checked += 1
    if state.has_activity:
        return

    row_date = str(item.get("date") or "")
    if not state.identity or row_date >= state.latest_row_date:
        state.identity = {name: item.get(name) for name in IDENTITY_FIELDS}
        state.identity["user_id"] = user_id
        state.latest_row_date = row_date

    for field_name in LAST_USAGE_FIELDS:
        value = item.get(field_name)
        if value in (None, ""):
            continue
        value_as_string = str(value)
        current_value = state.last_usage_dates[field_name]
        if current_value is None or value_as_string > current_value:
            state.last_usage_dates[field_name] = value_as_string

    if row_has_activity(item, start_date, end_date):
        state.has_activity = True
        # Identity and usage dates are not needed for users excluded from output.
        state.identity.clear()
        state.last_usage_dates.clear()


def stream_statistics(
    *,
    token: str,
    org_id: str,
    start_date: date,
    end_date: date,
    limit: int,
    timeout: int,
) -> tuple[dict[str, UserStatisticsState], int, int]:
    base_url = STATISTICS_URL.format(org_id=org_id)
    iteration_key = ""
    seen_iteration_keys: set[str] = set()
    states: dict[str, UserStatisticsState] = {}
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
            update_statistics_state(states, item, start_date, end_date)
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


def build_inactive_rows(
    *,
    states: dict[str, UserStatisticsState],
    user_departments: dict[str, str | None],
    departments: dict[str, str],
    start_date: date,
    end_date: date,
) -> tuple[list[dict[str, Any]], DepartmentLookupCounters]:
    rows: list[dict[str, Any]] = []
    counters = DepartmentLookupCounters()

    for user_id, state in states.items():
        if state.has_activity:
            continue
        department_id, department_name, lookup_status = resolve_department(
            user_id=user_id,
            user_departments=user_departments,
            departments=departments,
            counters=counters,
        )
        row = dict(state.identity)
        row.update(
            {
                "user_id": user_id,
                "department_id": department_id,
                "department_name": department_name,
                "department_lookup_status": lookup_status,
                "report_start_date": start_date.isoformat(),
                "report_end_date": end_date.isoformat(),
                "statistics_rows_checked": state.rows_checked,
            }
        )
        row.update(state.last_usage_dates)
        rows.append(row)

    rows.sort(
        key=lambda row: (
            str(row.get("department_name") or "").casefold(),
            str(row.get("name") or "").casefold(),
            str(row.get("nickname") or "").casefold(),
            str(row.get("user_id") or ""),
        )
    )
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
        validate_period(start_date, end_date)
        output_path = args.output or Path(
            f"inactive_y360_users_{start_date}_{end_date}.{args.format}"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        print("Получение справочника подразделений...", file=sys.stderr)
        departments, department_pages = fetch_departments(token, org_id, args.timeout)

        print("Получение справочника пользователей...", file=sys.stderr)
        user_departments, user_pages = fetch_user_departments(token, org_id, args.timeout)

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
            user_departments=user_departments,
            departments=departments,
            start_date=start_date,
            end_date=end_date,
        )

        if args.format == "csv":
            write_csv(output_path, inactive_rows)
        else:
            write_json(output_path, inactive_rows)

        print(f"Получено подразделений: {len(departments)} ({department_pages} стр.)")
        print(f"Получено пользователей справочника: {len(user_departments)} ({user_pages} стр.)")
        print(f"Получено строк статистики: {statistics_rows} ({statistics_pages} стр.)")
        print(f"Проверено пользователей статистики: {len(states)}")
        print(f"Пользователей без активности: {len(inactive_rows)}")
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
