#!/bin/sh
# Telegram-notify (once) when the capture daemon finds the WC final match
# event on Polymarket. Checks history first (in case it was already found),
# then follows the journal. Exits after sending.
set -u
PATTERN='\[final\] FOUND'
TG=/home/luppi/.local/bin/tg-send

line=$(journalctl --user -u polytrage-capture --since "-3 days" -o cat 2>/dev/null | grep -E "$PATTERN" | tail -1)
if [ -z "$line" ]; then
  line=$(journalctl --user -u polytrage-capture -f -o cat | grep -m1 -E "$PATTERN")
fi
"$TG" "⚽📈 polytrage: Polymarket just listed the World Cup final — ${line}

Depth capture attached automatically (3-way books recording through the game, Sunday Jul 19 19:00 UTC). Dashboard: https://polytrage.logicflow.co.il"
