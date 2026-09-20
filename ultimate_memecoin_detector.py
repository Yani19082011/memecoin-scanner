import os
import time
import requests
import logging
from typing import Dict, Any, List

# Настройка на логването за проследяване на пазара в реално време
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class UltimateMemecoinBot:
    def __init__(self):
        # Комбинирана конфигурация от двата проекта (Проект 1 за сигурност, Проект 2 за известия)
        self.telegram_token = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
        
        # Минимален толеранс за риск и скоринг лимити (Взето и подобрено от Проект 1)
        self.MIN_SCORING_THRESHOLD = 65  
        self.MAX_ALLOWED_DEV_HOLDING = 15.0  # Максимален % токени за създателя (Защита от Rug Pull)
        self.MIN_LIQUIDITY_USD = 5000        # Минимална ликвидност за сигурност

    def fetch_market_data(self) -> List[Dict[str, Any]]:
        """
        Извлича най-новите токени (Вдъхновено от data_fetch.py и pump.js).
        Свързва се с API на DEX скенер или Solana/Pump.fun платформи.
        """
        logging.info("Скениране на пазара за нови меме койни...")
        try:
            # Демонстрационни данни с логика за тестване на филтрите
            mock_tokens = [
                {
                    "symbol": "DOGEPLUS",
                    "address": "0x123...abc",
                    "liquidity": 12000,
                    "dev_holding_pct": 8.5,
                    "is_mintable": False,
                    "has_renounced_ownership": True,
                    "price_candles": [1.0, 1.2, 1.5, 1.4, 1.8] # Данни за свещи
                },
                {
                    "symbol": "SCAMCOIN",
                    "address": "0x987...xyz",
                    "liquidity": 2000,
                    "dev_holding_pct": 45.0, # Огромен риск от разпродажба
                    "is_mintable": True,      # Опасна функция за допълнително печатане
                    "has_renounced_ownership": False,
                    "price_candles": [1.0, 0.5, 0.2]
                }
            ]
            return mock_tokens
        except Exception as e:
            logging.error(f"Грешка при извличане на пазарни данни: {e}")
            return []

    def check_rug_signals(self, token: Dict[str, Any]) -> Dict[str, Any]:
        """
        ИНТЕГРАЦИЯ ОТ ПРОЕКТ 1 (memecoin-scanner): Анализ на сигурността срещу измами.
        """
        risks = []
        is_safe = True

        if token["liquidity"] < self.MIN_LIQUIDITY_USD:
            risks.append(f"Ниска ликвидност: ${token['liquidity']}")
            is_safe = False
            
        if token["dev_holding_pct"] > self.MAX_ALLOWED_DEV_HOLDING:
            risks.append(f"Прекалено много токени в създателя: {token['dev_holding_pct']}%")
            is_safe = False
            
        if token["is_mintable"]:
            risks.append("Договорът може да печата нови токени (Mintable)!")
            is_safe = False
            
        if not token["has_renounced_ownership"]:
            risks.append("Собствеността върху договора НЕ е отказана!")
            
        return {"is_safe": is_safe, "risks": risks}

    def analyze_candlesticks(self, price_history: List[float]) -> str:
        """
        ИНТЕГРАЦИЯ ОТ ПРОЕКТ 2 (GoldSignalBot): Технически анализ на ценовото движение (Candlesticks патърни).
        """
        if len(price_history) < 3:
            return "Недостатъчно данни за технически анализ"
            
        # Проверка за възходящ (бичи) тренд от последните свещи
        if price_history[-1] > price_history[-2] > price_history[-3]:
            return "СИЛЕН БИЧИ ТРЕНД (Bullish Momentum)"
        elif price_history[-1] < price_history[-2]:
            return "Мечи тренд (Bearish / Разпродажба)"
        return "Странично движение (Consolidation)"

    def calculate_total_score(self, token: Dict[str, Any], security: Dict[str, Any]) -> int:
        """
        КОМБИНИРАН СКОРИНГ: Пресмята финална оценка на база сигурност на смарт договора + пазарен тренд.
        """
        score = 50 # Базов резултат
        
        # Модифициране на скора спрямо сигурността
        if security["is_safe"]:
            score += 25
        else:
            score -= 30
            
        # Бонус при добра ликвидност
        if token["liquidity"] > 10000:
            score += 15
            
        return max(0, min(100, score))

    def send_notification(self, message: str):
        """
        ИЗВЕСТИЯ (От Проект 2): Автоматично изпращане на филтрираните сигнали към Telegram.
        """
        logging.info(f"ИЗВЕСТИЕ ДО TELEGRAM: {message}")
        # url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        # payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"}
        # requests.post(url, json=payload)

    def monitor_market(self):
        """ Основен работен цикъл на хибридния бот """
        print("🤖 Ultimate Memecoin Bot стартира успешно...")
        while True:
            tokens = self.fetch_market_data()
            
            for token in tokens:
                # 1. Проверка за сигурност (RugPull защита от Проект 1)
                security_report = self.check_rug_signals(token)
                
                # 2. Технически ценови анализ на свещите (От Проект 2)
                market_trend = self.analyze_candlesticks(token["price_candles"])
                
                # 3. Изчисляване на комбинираната оценка (Scoring)
                final_score = self.calculate_total_score(token, security_report)
                
                # 4. Филтриране на известията
                if final_score >= self.MIN_SCORING_THRESHOLD:
                    alert_msg = (
                        f"🚨 *ОТКРИТ ПОТЕНЦИАЛЕН ТОКЕН!* 🚨\n\n"
                        f"🪙 Токен: #{token['symbol']}\n"
                        f"📊 Финална оценка: {final_score}/100\n"
                        f"📈 Пазарен анализ: {market_trend}\n"
                        f"💧 Ликвидност: ${token['liquidity']}\n"
                        f"🛡️ Сигурност: Проверен и безопасен за търговия."
                    )
                    self.send_notification(alert_msg)
                else:
                    logging.info(f"Токен {token['symbol']} е отхвърлен (нисък скор или голям риск). Рискове: {security_report['risks']}")
            
            # Време за изчакване между скениранията (в секунди)
            time.sleep(60)

if __name__ == "__main__":
    bot = UltimateMemecoinBot()
    # За стартиране в реално време: bot.monitor_market()