"""
Асинхронен слушател на PumpPortal-ия безплатен public data WebSocket
(subscribeMigration = събития за "graduation" от bonding curve към
Raydium/PumpSwap). Няма нужда от API ключ за този stream (виж README.md).
"""
import asyncio
import json
import logging

import websockets

import config

log = logging.getLogger("pumpportal")

RECONNECT_DELAY_SECONDS = 10


async def listen_for_migrations(on_migration):
    """on_migration: async callback(event: dict), викан за всяко migration съобщение."""
    while True:
        try:
            async with websockets.connect(config.PUMPPORTAL_WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                log.info("Свързан към PumpPortal, чакам migration/graduation събития...")
                async for raw_message in ws:
                    try:
                        event = json.loads(raw_message)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and set(event.keys()) == {"message"}:
                        # PumpPortal-ското potvarждение за самия subscribe
                        # ("Subscribed to 'migration' events.") - НЕ е реално
                        # migration събитие, просто ACK на заявката ни отгоре.
                        # Преди го подавахме на on_migration() и той логваше
                        # объркващо "не разпознах mint адрес" за него - сега
                        # го разпознаваме тук и просто го логваме отделно.
                        log.info("PumpPortal потвърждение: %s", event.get("message"))
                        continue
                    log.info("RAW migration payload: %s", event)
                    await on_migration(event)
        except Exception as e:
            log.warning("PumpPortal връзката падна (%s) - reconnect след %ds.", e, RECONNECT_DELAY_SECONDS)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)
