#!/usr/bin/env python3
"""
Monitor de señales tempranas en altcoins.

Qué hace: cada vez que corre, baja el top de monedas de CoinGecko, compara contra
la corrida anterior y alerta por Telegram cuando detecta una combinación de:
  - volumen que se dispara vs la corrida previa
  - relación volumen / market cap anormalmente alta
  - movimiento de precio reciente, pero AÚN sin haber corrido demasiado (7d)

Uso:
  pip install requests
  export TELEGRAM_BOT_TOKEN="..."   # créalo con @BotFather
  export TELEGRAM_CHAT_ID="..."     # tu chat id (@userinfobot)
  python altcoin_monitor.py

Automatizar (cada 15 min):
  */15 * * * * /usr/bin/python3 /ruta/altcoin_monitor.py >> /ruta/monitor.log 2>&1

Esto NO es asesoría financiera ni predice nada: solo filtra ruido y te avisa rápido.
"""

import json
import os
import time
from pathlib import Path

import requests

# ---------------- CONFIGURACIÓN ----------------
MCAP_MIN = 100_000_000        # ignora monedas muy pequeñas (más manipulables)
MCAP_MAX = 8_000_000_000      # ignora gigantes (se mueven poco)
VOL_SPIKE_MULT = 2.5          # volumen actual >= 2.5x el de la corrida anterior
VOL_MCAP_MIN = 0.15           # volumen 24h / market cap mínimo
PRICE_1H_MIN = 3.0            # % mínimo de subida en 1h
PRICE_7D_MAX = 60.0           # si ya subió más de esto en 7d, se marca como "tarde"
SCORE_ALERT = 3               # puntaje mínimo para alertar (de 4)
COOLDOWN_HORAS = 12           # no repetir alerta de la misma moneda antes de esto
PAGES = 2                     # 250 monedas por página

STATE_FILE = Path(__file__).with_name("monitor_state.json")
API = "https://api.coingecko.com/api/v3/coins/markets"
# ------------------------------------------------


def fetch_market():
    coins = []
    for page in range(1, PAGES + 1):
        r = requests.get(
            API,
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 250,
                "page": page,
                "price_change_percentage": "1h,24h,7d",
            },
            timeout=30,
        )
        r.raise_for_status()
        coins.extend(r.json())
        time.sleep(2)  # respeta el límite del plan gratis
    return coins


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"volumes": {}, "alerted": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def score_coin(c, prev_vol):
    mcap = c.get("market_cap") or 0
    vol = c.get("total_volume") or 0
    ch1h = c.get("price_change_percentage_1h_in_currency") or 0
    ch7d = c.get("price_change_percentage_7d_in_currency") or 0

    if not (MCAP_MIN <= mcap <= MCAP_MAX):
        return 0, {}

    reasons = {}
    score = 0

    if prev_vol and vol >= prev_vol * VOL_SPIKE_MULT:
        score += 1
        reasons["volumen"] = f"{vol / prev_vol:.1f}x vs corrida anterior"

    if mcap and vol / mcap >= VOL_MCAP_MIN:
        score += 1
        reasons["vol/mcap"] = f"{vol / mcap:.2f}"

    if ch1h >= PRICE_1H_MIN:
        score += 1
        reasons["precio_1h"] = f"+{ch1h:.1f}%"

    if ch7d <= PRICE_7D_MAX:
        score += 1
        reasons["7d"] = f"{ch7d:+.1f}% (aún no corre mucho)"
    else:
        reasons["7d"] = f"{ch7d:+.1f}% (YA CORRIÓ, riesgo de llegar tarde)"

    return score, reasons


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[sin Telegram configurado]\n" + text)
        return
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text},
        timeout=20,
    )


def main():
    if os.environ.get("TEST_TELEGRAM") == "1":
        send_telegram("Monitor conectado correctamente. Las alertas llegaran aqui.")
        return
    state = load_state()
    now = time.time()
    coins = fetch_market()
    new_volumes = {}
    alerts = []

    for c in coins:
        cid = c["id"]
        new_volumes[cid] = c.get("total_volume") or 0
        prev = state["volumes"].get(cid)
        score, reasons = score_coin(c, prev)

        if score < SCORE_ALERT:
            continue
        last = state["alerted"].get(cid, 0)
        if now - last < COOLDOWN_HORAS * 3600:
            continue

        state["alerted"][cid] = now
        detalle = "\n".join(f"  - {k}: {v}" for k, v in reasons.items())
        alerts.append(
            f"{c['name']} ({c['symbol'].upper()}) | ${c['current_price']:,.4f} "
            f"| mcap ${c['market_cap'] / 1e6:,.0f}M | puntaje {score}/4\n{detalle}"
        )

    state["volumes"] = new_volumes
    save_state(state)

    if alerts:
        header = "ALERTA DE SEÑALES TEMPRANAS (no es recomendación de compra)\n\n"
        send_telegram(header + "\n\n".join(alerts))
    else:
        print("Sin señales en esta corrida.")


if __name__ == "__main__":
    main()
