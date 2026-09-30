#!/usr/bin/env python3
"""
Monitor de señales tempranas en altcoins (v2).

Cada corrida:
  1. Baja el top de monedas de CoinGecko.
  2. Excluye tokens meme.
  3. Compara el volumen 24h actual contra la mediana de las ultimas lecturas
     guardadas (una por hora, hasta 24). Sin historial suficiente NO alerta.
  4. Puntua 4 senales; alerta solo si llega a SCORE_ALERT (por defecto 4).
  5. Antes de alertar, verifica liquidez real en exchanges centralizados
     confiables (trust_score verde). Tokens que solo viven en DEX quedan fuera.

Variables de entorno: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Prueba de Telegram:   TEST_TELEGRAM=1

Herramienta informativa. No es asesoria financiera.
"""

import json
import os
import statistics
import time
from pathlib import Path

import requests

# ---------------- CONFIGURACION ----------------
MCAP_MIN = 100_000_000
MCAP_MAX = 8_000_000_000
VOL_SPIKE_MULT = 2.5          # volumen actual >= 2.5x la mediana historica
VOL_MCAP_MIN = 0.15           # volumen 24h / market cap
PRICE_1H_MIN = 3.0            # % minimo de subida en 1h
PRICE_7D_MAX = 60.0           # si ya subio mas en 7d, no cuenta como "temprano"
SCORE_ALERT = 4               # puntaje minimo (de 4)

MIN_READINGS = 6              # lecturas horarias necesarias antes de comparar
MAX_READINGS = 24             # historial que se conserva por moneda
READING_EVERY_SEC = 55 * 60   # una lectura por hora aprox.

MIN_GREEN_EXCHANGES = 2       # exchanges CEX con trust_score verde
MIN_GREEN_VOLUME_USD = 1_000_000
MAX_MEDIAN_SPREAD_PCT = 1.0   # spread bid/ask mediano maximo

COOLDOWN_HORAS = 12
PAGES = 2                     # 250 monedas por pagina
EXCLUDE_MEMES = True

STATE_FILE = Path(__file__).with_name("monitor_state.json")
BASE = "https://api.coingecko.com/api/v3"
# -----------------------------------------------


def get(path, params=None, retries=3):
    """GET con reintento si CoinGecko limita (429)."""
    for attempt in range(retries):
        r = requests.get(f"{BASE}{path}", params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(30 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"CoinGecko limito la consulta: {path}")


def fetch_market():
    coins = []
    for page in range(1, PAGES + 1):
        coins.extend(
            get(
                "/coins/markets",
                {
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": page,
                    "price_change_percentage": "1h,24h,7d",
                },
            )
        )
        time.sleep(2.5)
    return coins


def fetch_meme_ids():
    """Ids de tokens meme. Si falla, devuelve None (no se filtra)."""
    ids = set()
    try:
        for page in (1, 2):
            data = get(
                "/coins/markets",
                {
                    "vs_currency": "usd",
                    "category": "meme-token",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": page,
                },
            )
            ids.update(c["id"] for c in data)
            time.sleep(2.5)
        return ids
    except Exception as e:  # noqa: BLE001
        print(f"[aviso] no pude bajar la lista de memes: {e}")
        return None


def liquidity_check(coin_id):
    """Devuelve (ok, detalle). Mira exchanges centralizados confiables."""
    data = get(f"/coins/{coin_id}/tickers", {"order": "volume_desc"})
    green = {}
    spreads = []
    for t in data.get("tickers", []):
        if t.get("trust_score") != "green" or t.get("is_stale") or t.get("is_anomaly"):
            continue
        ex = (t.get("market") or {}).get("identifier")
        vol = (t.get("converted_volume") or {}).get("usd") or 0
        green[ex] = green.get(ex, 0) + vol
        sp = t.get("bid_ask_spread_percentage")
        if sp is not None:
            spreads.append(sp)

    n = len(green)
    total = sum(green.values())
    med_spread = statistics.median(spreads) if spreads else 99.0
    ok = (
        n >= MIN_GREEN_EXCHANGES
        and total >= MIN_GREEN_VOLUME_USD
        and med_spread <= MAX_MEDIAN_SPREAD_PCT
    )
    detalle = f"{n} exchanges confiables, ${total / 1e6:,.1f}M vol, spread {med_spread:.2f}%"
    return ok, detalle


def load_state():
    if STATE_FILE.exists():
        s = json.loads(STATE_FILE.read_text())
    else:
        s = {}
    s.setdefault("hist", {})      # {id: [[ts, vol], ...]}
    s.setdefault("alerted", {})
    s.pop("volumes", None)        # formato viejo (v1)
    return s


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, separators=(",", ":")))


def baseline(readings):
    if len(readings) < MIN_READINGS:
        return None
    return statistics.median(v for _, v in readings)


def score_coin(c, base_vol):
    mcap = c.get("market_cap") or 0
    vol = c.get("total_volume") or 0
    ch1h = c.get("price_change_percentage_1h_in_currency") or 0
    ch7d = c.get("price_change_percentage_7d_in_currency") or 0

    if not (MCAP_MIN <= mcap <= MCAP_MAX):
        return 0, {}

    score, reasons = 0, {}

    if base_vol and vol >= base_vol * VOL_SPIKE_MULT:
        score += 1
        reasons["volumen"] = f"{vol / base_vol:.1f}x vs mediana reciente"
    if mcap and vol / mcap >= VOL_MCAP_MIN:
        score += 1
        reasons["vol/mcap"] = f"{vol / mcap:.2f}"
    if ch1h >= PRICE_1H_MIN:
        score += 1
        reasons["precio_1h"] = f"+{ch1h:.1f}%"
    if ch7d <= PRICE_7D_MAX:
        score += 1
        reasons["7d"] = f"{ch7d:+.1f}% (aun no corre mucho)"
    return score, reasons


def update_history(state, coins, now):
    keep = {}
    for c in coins:
        mcap = c.get("market_cap") or 0
        if not (MCAP_MIN <= mcap <= MCAP_MAX):
            continue
        cid = c["id"]
        readings = state["hist"].get(cid, [])
        if not readings or now - readings[-1][0] >= READING_EVERY_SEC:
            readings.append([int(now), int(c.get("total_volume") or 0)])
        keep[cid] = readings[-MAX_READINGS:]
    state["hist"] = keep


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
    memes = fetch_meme_ids() if EXCLUDE_MEMES else set()

    candidates = []
    for c in coins:
        cid = c["id"]
        if memes and cid in memes:
            continue
        if now - state["alerted"].get(cid, 0) < COOLDOWN_HORAS * 3600:
            continue
        base = baseline(state["hist"].get(cid, []))
        score, reasons = score_coin(c, base)
        if score >= SCORE_ALERT:
            candidates.append((c, score, reasons))

    alerts = []
    for c, score, reasons in candidates:
        try:
            ok, liq = liquidity_check(c["id"])
        except Exception as e:  # noqa: BLE001
            print(f"[aviso] liquidez no verificada para {c['id']}: {e}")
            continue
        time.sleep(2.5)
        if not ok:
            print(f"[descartada por liquidez] {c['id']}: {liq}")
            continue
        state["alerted"][c["id"]] = now
        detalle = "\n".join(f"  - {k}: {v}" for k, v in reasons.items())
        alerts.append(
            f"{c['name']} ({c['symbol'].upper()}) | ${c['current_price']:,.4f} "
            f"| mcap ${c['market_cap'] / 1e6:,.0f}M | puntaje {score}/4\n"
            f"{detalle}\n  - liquidez: {liq}"
        )

    update_history(state, coins, now)
    save_state(state)

    if memes is None:
        print("[aviso] filtro de memes no aplicado en esta corrida")
    if alerts:
        header = "ALERTA DE SEÑALES TEMPRANAS (no es recomendacion de compra)\n\n"
        send_telegram(header + "\n\n".join(alerts))
    else:
        print("Sin señales en esta corrida.")


if __name__ == "__main__":
    main()
