from __future__ import annotations

from datetime import datetime
from pathlib import Path

from weather_arb import config
from weather_arb.utils.io_utils import safe_read_json


def _fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def generate_daily_report(date_key: str, out_dir: Path | None = None) -> Path:
    out_root = out_dir or config.REPORTS_DIR
    out_root.mkdir(parents=True, exist_ok=True)

    paper = safe_read_json(config.PAPER_METRICS_DAILY_PATH) or {}
    live = safe_read_json(config.LIVE_METRICS_DAILY_PATH) or {}
    registry = safe_read_json(config.MODEL_REGISTRY_PATH) or {}

    paper_day = dict((paper.get("by_day") or {}).get(date_key, {}))
    live_day = dict((live.get("by_day") or {}).get(date_key, {}))

    champion_id = ((registry.get("champion_by_scope") or {}).get("global") or "none")

    lines = [
        f"# Weather Model Daily Report ({date_key})",
        "",
        f"Generated at: {datetime.utcnow().isoformat(timespec='seconds')}Z",
        "",
        "## Registry",
        f"- Champion model: `{champion_id}`",
        f"- Total models: {len(registry.get('models', []))}",
        "",
        "## Paper",
        f"- Trades: {int(paper_day.get('trades', 0) or 0)}",
        f"- Win rate: {float(paper_day.get('win_rate', 0.0) or 0.0):.2%}",
        f"- PnL: {_fmt_money(float(paper_day.get('pnl_dollars', 0.0) or 0.0))}",
        f"- ROI/trade: {float(paper_day.get('roi_per_trade', 0.0) or 0.0):.4f}",
        "",
        "## Live",
        f"- Trades: {int(live_day.get('trades', 0) or 0)}",
        f"- Win rate: {float(live_day.get('win_rate', 0.0) or 0.0):.2%}",
        f"- PnL: {_fmt_money(float(live_day.get('pnl_dollars', 0.0) or 0.0))}",
        f"- ROI/trade: {float(live_day.get('roi_per_trade', 0.0) or 0.0):.4f}",
        "",
    ]

    out_path = out_root / f"daily_report_{date_key}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
