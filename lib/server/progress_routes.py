from __future__ import annotations

from typing import Any, Dict

from fastapi import FastAPI, HTTPException


def register_progress_routes(app: FastAPI) -> None:
    @app.get("/api/progress/{operation_id}")
    def operation_progress(operation_id: str) -> Dict[str, Any]:
        snapshot = app.state.progress_registry.get(operation_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Progress operation not found")
        return snapshot


__all__ = ["register_progress_routes"]
