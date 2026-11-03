#!/usr/bin/env python3
"""Export all rows from databases nested under the Notion Parent databases page."""

from __future__ import annotations

import argparse
import json
import csv
import io
import os
import re
import shutil
import sys
import time
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://api.notion.com/v1"
API_VERSION = os.getenv("NOTION_API_VERSION", "2026-03-11")
DEFAULT_ROOT_PAGE_ID = "74134a16-75a8-8356-9416-817fd7fc8825"
DEFAULT_PARENT_PAGE_ID = "97c34a16-75a8-8354-aebb-814dbf396371"
STATUS_PATH: Path | None = None
SERVE_DASHBOARD = False
OUTPUT_PATH: Path | None = None


class NotionAPIError(RuntimeError):
    pass


class NotionClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            API_BASE + path,
            data=payload,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": API_VERSION,
                "Content-Type": "application/json",
                "User-Agent": "notion-calendar-analytics-export/1.0",
            },
        )
        for attempt in range(6):
            try:
                with urlopen(request, timeout=60) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and attempt < 5:
                    wait = int(exc.headers.get("Retry-After", "2"))
                    time.sleep(max(wait, 1))
                    continue
                raise NotionAPIError(f"Notion API {exc.code} at {path}: {detail}") from exc
            except URLError as exc:
                if attempt < 5:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise NotionAPIError(f"Network error at {path}: {exc}") from exc
        raise NotionAPIError(f"Request retries exhausted at {path}")

    def children(self, block_id: str) -> list[dict]:
        rows: list[dict] = []
        cursor = None
        while True:
            path = f"/blocks/{block_id}/children?page_size=100"
            if cursor:
                path += "&" + urlencode({"start_cursor": cursor})
            response = self.request("GET", path)
            rows.extend(response.get("results", []))
            if not response.get("has_more"):
                return rows
            cursor = response["next_cursor"]

    def data_source_rows(self, data_source_id: str) -> list[dict]:
        rows: list[dict] = []
        cursor = None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            response = self.request("POST", f"/data_sources/{data_source_id}/query", body)
            rows.extend(response.get("results", []))
            if not response.get("has_more"):
                return rows
            cursor = response["next_cursor"]


def clean_id(value: str) -> str:
    match = re.search(r"[0-9a-fA-F]{32}", value)
    if match:
        raw = match.group(0).lower()
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    match = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", value)
    if match:
        return match.group(0).split("?")[0].split("#")[0]
    raise ValueError(f"Cannot extract a Notion ID from: {value}")


def title_of_database(block: dict) -> str:
    content = block.get("child_database", {})
    title = content.get("title") or "未命名数据库"
    return title.strip() if isinstance(title, str) else "未命名数据库"


def discover_databases(client: NotionClient, page_id: str) -> list[dict]:
    found: dict[str, dict] = {}
    visited: set[str] = set()

    def walk(block_id: str) -> None:
        if block_id in visited:
            return
        visited.add(block_id)
        for block in client.children(block_id):
            kind = block.get("type")
            if kind == "child_database":
                db_id = block["id"]
                found[db_id] = {"id": db_id, "title": title_of_database(block)}
            elif kind == "child_page":
                # Parent databases uses nested pages/toggles to group its inline databases.
                walk(block["id"])
            if block.get("has_children") and kind not in {"child_page", "child_database"}:
                walk(block["id"])

    walk(page_id)
    return sorted(found.values(), key=lambda item: item["title"].casefold())


def safe_filename(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return value[:100] or "database"


def prop_text(value: dict) -> str:
    """Flatten a Notion property into a stable human-readable CSV cell."""
    kind = value.get("type")
    data = value.get(kind) if kind else value
    if kind in {"title", "rich_text"}:
        return "".join(x.get("plain_text") or x.get("text", {}).get("content", "") for x in (data or []))
    if kind in {"select", "status"}:
        return (data or {}).get("name", "") if isinstance(data, dict) else ""
    if kind == "multi_select":
        return "; ".join(x.get("name", "") for x in (data or []))
    if kind == "date":
        return (data or {}).get("start", "") if isinstance(data, dict) else ""
    if kind == "formula":
        if not isinstance(data, dict): return ""
        return prop_text({"type": data.get("type"), data.get("type"): data.get(data.get("type"))})
    if kind == "number":
        return "" if data is None else str(data)
    if kind == "url": return data or ""
    if kind == "checkbox": return "是" if data else "否"
    if kind == "people": return "; ".join(x.get("name", x.get("id", "")) for x in (data or []))
    if kind == "relation": return "; ".join(x.get("id", "") for x in (data or []))
    if kind == "files": return "; ".join(x.get("name", "") for x in (data or []))
    if kind == "rollup":
        if isinstance(data, dict): return str(data.get("number", data.get("date", "")) or "")
    return "" if data is None else str(data)


def database_csv(database: dict, source: dict, rows: list[dict]) -> str:
    core = ["Event", "Event Date Start", "Event Date End", "Time allotted", "Select", "WK #", "Year", "Source Database", "Source Data Source", "Page ID", "Source URL"]
    property_names = sorted({name for row in rows for name in row.get("properties", {}) if name not in core})
    headers = core + property_names
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=headers, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        props = row.get("properties", {})
        values = {name: prop_text(props.get(name, {})) for name in property_names}
        title_name = next((name for name, val in props.items() if val.get("type") == "title"), "")
        event = prop_text(props.get("Event", props.get(title_name, {})))
        dateval = props.get("Event Date", props.get("Date", props.get("日期", {})))
        dateobj = dateval.get("date") if dateval.get("type") == "date" else {}
        duration = props.get("Time allotted", props.get("Duration", props.get("时长", {})))
        select = props.get("Select", props.get("Category", props.get("类别", {})))
        values.update({
            "Event": event,
            "Event Date Start": (dateobj or {}).get("start", ""),
            "Event Date End": (dateobj or {}).get("end", ""),
            "Time allotted": prop_text(duration),
            "Select": prop_text(select),
            "WK #": prop_text(props.get("WK #", props.get("Week #", {}))),
            "Year": prop_text(props.get("Year", {})),
            "Source Database": database.get("title", ""),
            "Source Data Source": source.get("name", ""),
            "Page ID": row.get("id", ""),
            "Source URL": row.get("url", ""),
        })
        writer.writerow(values)
    return stream.getvalue()


def write_sync_status(state: str, message: str = "") -> None:
    if STATUS_PATH is None:
        return
    target = STATUS_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream); writer.writerow(["status", "checked_at", "message"]); writer.writerow([state, datetime.now(timezone.utc).isoformat(), message])
    except OSError:
        pass


def main() -> int:
    global STATUS_PATH, SERVE_DASHBOARD, OUTPUT_PATH
    parser = argparse.ArgumentParser(description="Export rows from the Notion Calendar Analytics parent databases.")
    parser.add_argument("--root-page-id", default=os.getenv("NOTION_ROOT_PAGE_ID", DEFAULT_ROOT_PAGE_ID), help="Calendar Analytics template page ID")
    parser.add_argument("--parent-page-id", default=os.getenv("NOTION_PARENT_PAGE_ID", DEFAULT_PARENT_PAGE_ID), help="Parent databases page ID")
    parser.add_argument("--output-dir", default=os.getenv("NOTION_EXPORT_DIR", "exports/latest"), help="Folder atomically replaced with the latest successful export")
    parser.add_argument("--serve-dashboard", action="store_true", help="Open the local dashboard after syncing and keep it available")
    args = parser.parse_args()
    SERVE_DASHBOARD = args.serve_dashboard
    OUTPUT_PATH = Path(args.output_dir).expanduser().resolve()
    STATUS_PATH = Path(args.output_dir).expanduser().resolve() / "sync_status.csv"

    token = os.getenv("NOTION_API_KEY")
    if not token:
        print("Missing NOTION_API_KEY. Create an internal Notion integration, share the target pages with it, and set this environment variable.", file=sys.stderr)
        write_sync_status("failed", "未设置 NOTION_API_KEY；请先配置 Notion API 密钥")
        return 2

    client = NotionClient(token)
    parent_id = clean_id(args.parent_page_id) if args.parent_page_id else clean_id(args.root_page_id)
    if not args.parent_page_id:
        # Locate the named Parent databases page from the template's block tree.
        pending = [parent_id]
        seen: set[str] = set()
        match_id = None
        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            for block in client.children(current):
                kind = block.get("type")
                if kind == "child_page" and block.get("child_page", {}).get("title", "").strip().casefold() == "parent databases":
                    match_id = block["id"]
                    break
                if block.get("has_children") and kind not in {"child_database"}:
                    pending.append(block["id"])
            if match_id:
                break
        if not match_id:
            raise NotionAPIError("Could not find the 'Parent databases' page under the configured template. Check that the integration can access the template page.")
        parent_id = match_id

    databases = discover_databases(client, parent_id)
    if not databases:
        raise NotionAPIError("No child databases were found under Parent databases. Share each source database with the integration and try again.")

    run_dir = Path(args.output_dir).expanduser().resolve()
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_dir = run_dir.with_name(run_dir.name + ".new")
    backup_dir = run_dir.with_name(run_dir.name + ".previous")
    if backup_dir.exists():
        if not run_dir.exists():
            os.replace(backup_dir, run_dir)
        else:
            shutil.rmtree(backup_dir)
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "notion_api_version": API_VERSION,
        "root_page_id": clean_id(args.root_page_id),
        "parent_databases_page_id": parent_id,
        "database_count": len(databases),
        "databases": [],
    }

    for database in databases:
        db_id = database["id"]
        db_info = client.request("GET", f"/databases/{db_id}")
        sources = db_info.get("data_sources", [])
        exported_sources = []
        for source in sources:
            source_id = source["id"]
            rows = client.data_source_rows(source_id)
            source_name = source.get("name") or database["title"]
            filename = f"{safe_filename(database['title'])}__{safe_filename(source_name)}.csv"
            file_path = stage_dir / filename
            file_path.write_text(database_csv(database, source, rows), encoding="utf-8-sig", newline="")
            exported_sources.append({"id": source_id, "name": source_name, "row_count": len(rows), "file": filename})
            print(f"导出 {database['title']} / {source_name}: {len(rows)} 条")
        manifest["databases"].append({**database, "data_sources": exported_sources})

    with (stage_dir / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream); writer.writerow(["Database", "Database ID", "Data Source", "Data Source ID", "Rows", "CSV File", "Exported At"])
        for db in manifest["databases"]:
            for source in db["data_sources"]: writer.writerow([db["title"], db["id"], source["name"], source["id"], source["row_count"], source["file"], manifest["exported_at"]])
    with (stage_dir / "sync_status.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream); writer.writerow(["status", "checked_at", "message"]); writer.writerow(["success", manifest["exported_at"], ""])
    if run_dir.exists():
        os.replace(run_dir, backup_dir)
    try:
        os.replace(stage_dir, run_dir)
    except Exception:
        if backup_dir.exists() and not run_dir.exists():
            os.replace(backup_dir, run_dir)
        raise
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    print(f"完成：{len(databases)} 个数据库；已覆盖更新：{run_dir}")
    return 0


def serve_dashboard() -> None:
    """Serve the dashboard and current export locally; the browser never sees the Notion token."""
    assert OUTPUT_PATH is not None
    dashboard = Path(sys.executable).resolve().parent / "time_dashboard.html"
    if not dashboard.exists():
        dashboard = Path(__file__).resolve().with_name("time_dashboard.html")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/api/colors":
                settings = dashboard.with_name("dashboard_settings.csv")
                try:
                    payload = settings.read_bytes()
                except OSError:
                    payload = b"\xef\xbb\xbfcategory,color\r\n"
                content_type = "text/csv; charset=utf-8"
            elif self.path == "/api/data":
                files = []
                for path in sorted(OUTPUT_PATH.glob("*.csv")):
                    if path.name in {"manifest.csv", "sync_status.csv"}:
                        continue
                    try:
                        files.append({"name": path.name, "csv": path.read_text(encoding="utf-8-sig")})
                    except OSError:
                        continue
                payload = json.dumps({"files": files}, ensure_ascii=False).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            elif self.path == "/api/status":
                status_file = OUTPUT_PATH / "sync_status.csv"
                manifest_file = OUTPUT_PATH / "manifest.csv"
                try:
                    with status_file.open(encoding="utf-8-sig", newline="") as stream: status = next(csv.DictReader(stream), {})
                except OSError:
                    status = {"status": "failed", "message": "尚未成功导出 Notion 数据", "checked_at": ""}
                try:
                    with manifest_file.open(encoding="utf-8-sig", newline="") as stream: manifest = list(csv.DictReader(stream))
                except OSError:
                    manifest = []
                payload = json.dumps({"sync": status, "manifest": manifest}, ensure_ascii=False).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            elif self.path in {"/", "/time_dashboard.html"}:
                try:
                    payload = dashboard.read_bytes()
                except OSError:
                    self.send_error(404, "time_dashboard.html not found next to the app")
                    return
                content_type = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if self.path != "/api/colors" or self.headers.get("Origin") != expected_origin:
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 1_000_000:
                    self.send_error(413)
                    return
                raw = self.rfile.read(length).decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(raw))
                settings = dashboard.with_name("dashboard_settings.csv")
                with settings.open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.writer(stream, lineterminator="\r\n")
                    writer.writerow(["category", "color"])
                    for row in reader:
                        category, color = row.get("category", ""), row.get("color", "")
                        if category and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                            writer.writerow([category, color.lower()])
            except (OSError, UnicodeDecodeError, csv.Error):
                self.send_error(400)
                return
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def log_message(self, fmt: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"本机网页已启动：{url}（关闭此窗口即可退出）")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    try:
        result = main()
    except (NotionAPIError, ValueError, OSError) as exc:
        write_sync_status("failed", str(exc))
        print(f"导出失败：{exc}", file=sys.stderr)
        result = 1
    if SERVE_DASHBOARD:
        serve_dashboard()
    raise SystemExit(result)
