from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


logger = logging.getLogger("html_brief_log")
logger_ui = logging.getLogger("ui_logger")


class ChartGenerateRequest(BaseModel):
    force: bool = False
    operation_id: Optional[str] = None


def register_chart_routes(app: FastAPI) -> None:
    """Register chart status and generation endpoints."""

    def require_bms_config() -> Any:
        if app.state.bms_cfg is None:
            raise HTTPException(status_code=500, detail="BMS config is not loaded. Reload and try again.")
        return app.state.bms_cfg

    @app.get("/api/charts/status")
    def chart_status() -> Dict[str, Any]:
        bms_cfg = require_bms_config()
        try:
            return app.state.chart_service.status(
                app.state.cfg,
                bms_cfg,
                failures=app.state.chart_failures,
                generating=app.state.chart_generating,
            )
        except Exception as exc:
            logger.exception("Failed to inspect chart status")
            raise HTTPException(status_code=500, detail=f"Failed to inspect chart status: {exc}") from exc

    @app.post("/api/charts/generate")
    def generate_charts(payload: ChartGenerateRequest) -> Dict[str, Any]:
        bms_cfg = require_bms_config()
        if app.state.pdf_busy:
            raise HTTPException(status_code=429, detail="PDF generation is already in progress.")
        lock = app.state.chart_lock
        if not lock.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="Chart generation is already in progress.")
        operation_id = payload.operation_id
        progress = app.state.progress_registry.begin(
            operation_id,
            operation="charts",
            title="Generating charts",
            message="Resolving selected charts...",
            note="Only done once per chart. Only runs again when the source data or chart generator updates.",
        )
        try:
            plan = app.state.chart_service.build_plan(
                app.state.cfg,
                bms_cfg,
                fresh_source=True,
            )
            app.state.chart_generating = {target.id for target in plan.targets}
            app.state.chart_failures = {}
            result = app.state.chart_service.generate(
                app.state.cfg,
                bms_cfg,
                force=payload.force,
                prepared_plan=plan,
                progress=progress,
            )
            app.state.chart_failures = dict(result.get("failures") or {})
            for warning in result.get("warnings") or []:
                logger_ui.warning(warning)
            response = app.state.chart_service.status(
                app.state.cfg,
                bms_cfg,
                failures=app.state.chart_failures,
            )
            response["result"] = result
            failures = len(result.get("failures") or {})
            app.state.progress_registry.complete(
                operation_id,
                (
                    f"Chart generation finished with {failures} failure{'s' if failures != 1 else ''}."
                    if failures
                    else "Charts are ready."
                ),
            )
            return response
        except HTTPException as exc:
            app.state.progress_registry.fail(operation_id, str(exc.detail))
            raise
        except Exception as exc:
            app.state.progress_registry.fail(operation_id, f"Chart generation failed: {exc}")
            logger.exception("Chart generation failed")
            logger_ui.error("Chart generation failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"Chart generation failed: {exc}") from exc
        finally:
            app.state.chart_generating = set()
            lock.release()


__all__ = ["ChartGenerateRequest", "register_chart_routes"]
