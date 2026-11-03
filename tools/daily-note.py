#!/usr/bin/env python3
"""Creates today's daily note from template if it doesn't already exist."""

import sys
from datetime import date
from pathlib import Path

VAULT = Path.cwd()
DAILY_DIR = VAULT / "01-daily"
TEMPLATE = VAULT / "90-meta" / "Szablony" / "Daily.md"

def main() -> int:
    today = date.today()
    filename = f"{today.isoformat()}.md"
    target = DAILY_DIR / filename

    if target.exists():
        print(f"Already exists: {target.relative_to(VAULT)}")
        return 0

    template_text = TEMPLATE.read_text(encoding="utf-8")

    day_pl = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]
    month_pl = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
                "lipca", "sierpnia", "września", "października", "listopada", "grudnia"]

    date_long = f"{day_pl[today.weekday()]}, {today.day} {month_pl[today.month - 1]} {today.year}"

    content = template_text.replace("{{date:YYYY-MM-DD}}", today.isoformat())
    content = content.replace("{{date:dddd, D MMMM YYYY}}", date_long)

    target.write_text(content, encoding="utf-8")
    print(f"Created: {target.relative_to(VAULT)}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
