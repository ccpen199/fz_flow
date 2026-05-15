import errno
import json
import mimetypes
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

from app.config_store import (
    add_field,
    add_relation,
    create_scene,
    ensure_preset_scenes,
    get_scene,
    list_scenes,
)
from app.engine import export_ppt, run_analysis
from app.mock_data import ensure_demo_database

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "app" / "static"
OUTPUT = ROOT / "output"
SESSIONS: Dict[str, Dict[str, Any]] = {}


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> Dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(size) if size > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def _serve_file(self, path: Path) -> None:
        if not path.exists():
            self._send_json({"error": "not found"}, status=404)
            return
        body = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/ui/", "/ui"):
            self._serve_file(STATIC / "index.html")
            return
        if self.path == "/api/health":
            self._send_json({"status": "ok", "mode": "offline-stdlib"})
            return
        if self.path == "/api/config/relation-suggestions":
            self._send_json(
                {
                    "suggestions": [
                        {
                            "left_table": "product_snapshot",
                            "left_field": "brand_id",
                            "right_table": "dim_brand",
                            "right_field": "brand_id",
                            "join_type": "INNER",
                            "confidence": 0.98,
                            "reason": "字段名一致且维表主键匹配",
                        },
                        {
                            "left_table": "product_snapshot",
                            "left_field": "platform_id",
                            "right_table": "dim_platform",
                            "right_field": "platform_id",
                            "join_type": "INNER",
                            "confidence": 0.98,
                            "reason": "字段名一致且维表主键匹配",
                        },
                        {
                            "left_table": "product_snapshot",
                            "left_field": "category_id",
                            "right_table": "dim_category",
                            "right_field": "category_id",
                            "join_type": "INNER",
                            "confidence": 0.98,
                            "reason": "字段名一致且维表主键匹配",
                        },
                    ]
                }
            )
            return
        if self.path == "/api/config/scenes":
            self._send_json({"scenes": list_scenes()})
            return
        if self.path.startswith("/api/config/scenes/"):
            scene_id = self.path.split("/")[-1]
            scene = get_scene(scene_id)
            if not scene:
                self._send_json({"error": "Scene not found"}, status=404)
                return
            self._send_json(scene)
            return
        if self.path.startswith("/api/sessions/"):
            session_id = self.path.split("/")[-1]
            session = SESSIONS.get(session_id)
            if not session:
                self._send_json({"error": "Session not found"}, status=404)
                return
            self._send_json(session)
            return
        if self.path.startswith("/api/download/"):
            session_id = self.path.split("/")[-1]
            cand = list(OUTPUT.glob(f"analysis_{session_id}.*"))
            if not cand:
                self._send_json({"error": "report not found"}, status=404)
                return
            self._serve_file(cand[0])
            return
        if self.path.startswith("/ui/"):
            rel = self.path.replace("/ui/", "", 1)
            self._serve_file(STATIC / rel)
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/config/scenes":
            body = self._read_json()
            name = str(body.get("name", "")).strip()
            description = str(body.get("description", "")).strip()
            if not name:
                self._send_json({"error": "scene name is required"}, status=400)
                return
            self._send_json(create_scene(name, description), status=201)
            return
        if self.path == "/api/config/scenes/preset-sync":
            scenes = ensure_preset_scenes()
            self._send_json({"scenes": scenes, "count": len(scenes)}, status=200)
            return
        if self.path.startswith("/api/config/scenes/") and self.path.endswith("/fields"):
            parts = self.path.split("/")
            scene_id = parts[4]
            body = self._read_json()
            try:
                created = add_field(
                    scene_id=scene_id,
                    table_name=str(body.get("table_name", "")),
                    field_name=str(body.get("field_name", "")),
                    semantic_name=str(body.get("semantic_name", "")),
                    description=str(body.get("description", "")),
                    role=str(body.get("role", "dimension")),
                    enabled=bool(body.get("enabled", True)),
                )
                self._send_json(created, status=201)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=404)
            return
        if self.path.startswith("/api/config/scenes/") and self.path.endswith("/relations"):
            parts = self.path.split("/")
            scene_id = parts[4]
            body = self._read_json()
            try:
                created = add_relation(
                    scene_id=scene_id,
                    left_table=str(body.get("left_table", "")),
                    left_field=str(body.get("left_field", "")),
                    right_table=str(body.get("right_table", "")),
                    right_field=str(body.get("right_field", "")),
                    join_type=str(body.get("join_type", "INNER")),
                    note=str(body.get("note", "")),
                )
                self._send_json(created, status=201)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=404)
            return
        if self.path == "/api/sessions":
            session_id = uuid4().hex[:12]
            SESSIONS[session_id] = {
                "session_id": session_id,
                "goals": [],
                "results": [],
                "slides": [],
            }
            self._send_json({"session_id": session_id}, status=201)
            return
        if self.path.startswith("/api/sessions/") and self.path.endswith("/analyze"):
            parts = self.path.split("/")
            session_id = parts[3]
            session = SESSIONS.get(session_id)
            if not session:
                self._send_json({"error": "Session not found"}, status=404)
                return
            body = self._read_json()
            goal = str(body.get("goal", "")).strip()
            if not goal:
                self._send_json({"error": "goal is empty"}, status=400)
                return
            result = run_analysis(goal)
            pack = {
                "goal": result.goal,
                "query_plan": result.query_plan,
                "sql": result.sql,
                "columns": result.columns,
                "rows": result.rows,
                "insight": result.insight,
                "recommendation": result.recommendation,
                "slide": result.slide,
            }
            session["goals"].append(goal)
            session["results"].append(pack)
            session["slides"].append(result.slide)
            self._send_json(pack)
            return
        if self.path.startswith("/api/sessions/") and self.path.endswith("/export"):
            parts = self.path.split("/")
            session_id = parts[3]
            session = SESSIONS.get(session_id)
            if not session:
                self._send_json({"error": "Session not found"}, status=404)
                return
            slides = session.get("slides", [])
            if not slides:
                self._send_json({"error": "No slides to export"}, status=400)
                return
            file_path = export_ppt(session_id, slides)
            self._send_json(
                {"file_path": file_path, "download_url": f"/api/download/{session_id}"}
            )
            return
        self._send_json({"error": "not found"}, status=404)


def main() -> None:
    ensure_demo_database()
    ensure_preset_scenes()
    host = os.getenv("HOST", "0.0.0.0")
    # Ignore ambient PORT (often injected by shells/IDEs) to keep demo stable.
    # Use DEMO_PORT for overrides.
    desired_port = int(os.getenv("DEMO_PORT", "8787"))
    server = None
    active_port = desired_port
    for port in range(desired_port, desired_port + 20):
        try:
            server = ThreadingHTTPServer((host, port), Handler)
            active_port = port
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    if server is None:
        raise RuntimeError(
            f"Failed to bind demo server on {host}:{desired_port}~{desired_port + 19}"
        )
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print("Offline demo server started")
    print(f"Open UI: http://{display_host}:{active_port}/ui/")
    print(f"Health: http://{display_host}:{active_port}/api/health")
    if active_port != desired_port:
        print(f"Port {desired_port} unavailable, switched to {active_port}.")
    server.serve_forever()


if __name__ == "__main__":
    main()
