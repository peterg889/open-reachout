"""Open Reachout: agentic, compliant outbound outreach framework."""

__version__ = "0.0.1"

# Core constants that are commitments, not configuration (PRD FR-3.5, FR-7.3).
# These are deliberately module-level in the package root: lowering them via
# config is allowed elsewhere; nothing may raise them.
MAX_FOLLOW_UPS = 3
MIN_FOLLOW_UP_GAP_DAYS = 3
DEFAULT_MIN_CAMPAIGN_GAP_DAYS = 90
DEFAULT_ANNUAL_TOUCH_CAP = 8
DEFAULT_DAILY_INBOX_CAP = 25
