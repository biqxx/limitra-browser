from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import structlog
from playwright.async_api import BrowserContext, Page

from app.config import get_settings

logger = structlog.get_logger(__name__)


class DiagnosticManager:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.local_diag_dir = Path("diagnostics")
        self.local_diag_dir.mkdir(parents=True, exist_ok=True)

    async def capture_artifacts(
        self,
        provider: str,
        operation: str,
        context: BrowserContext | None = None,
        page: Page | None = None,
        job_id: str | None = None,
    ) -> dict[str, str]:
        """
        Captures diagnostic artifacts (screenshot, HTML, traces) if diagnostic mode is active.
        """
        if not self.settings.DIAGNOSTIC_MODE_ENABLED:
            return {}

        results: dict[str, str] = {}
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        prefix = f"{provider}_{operation}_{job_id or 'sync'}_{ts}"


        try:
            if page and not page.is_closed():
                # 1. Capture HTML snapshot
                try:
                    html_content = await page.content()
                    html_file = self.local_diag_dir / f"{prefix}.html"
                    html_file.write_text(html_content, encoding="utf-8")
                    results["html"] = str(html_file)
                except Exception as e:
                    logger.warning("failed_to_capture_html_diagnostic", error=str(e))

                # 2. Capture Screenshot
                try:
                    screenshot_bytes = await page.screenshot(full_page=False)
                    screenshot_file = self.local_diag_dir / f"{prefix}.png"
                    screenshot_file.write_bytes(screenshot_bytes)
                    results["screenshot"] = str(screenshot_file)
                except Exception as e:
                    logger.warning("failed_to_capture_screenshot_diagnostic", error=str(e))

            if context:
                # 3. Stop tracing if was started
                try:
                    trace_file = self.local_diag_dir / f"{prefix}_trace.zip"
                    await context.tracing.stop(path=str(trace_file))
                    results["trace"] = str(trace_file)
                except Exception:
                    pass  # tracing might not have been enabled

        except Exception as e:
            logger.error("diagnostic_capture_error", error=str(e), provider=provider)

        return results
