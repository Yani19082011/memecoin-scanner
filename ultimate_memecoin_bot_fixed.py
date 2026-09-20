import os
import time
import requests
import logging
import resend  # Използваме официалната библиотека на Resend
from typing import Dict, Any, List

# Настройка на логването за проследяване на пазара в реално време
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class UltimateMemecoinBot:
    def __init__(self):
        self.telegram_token = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
        
        # ДИНАМИЧЕН СПИСЪК С ПОЛУЧАТЕЛИ НА ИМЕЙЛ ИЗВЕСТИЯТА
        # Взима стойността от Render Environment (разделени със запетая имейли)
        emails_env = os.getenv("ALERT_EMAIL_TO")
        if emails_env:
            self.receiver_emails = [email.strip() for email in emails_env.split(",") if email.strip()]
        else:
            self.receiver_emails = ["yani.kolev2011@gmail.com", "crafts0man0@gmail.com"]
        
        # Автоматично взимаме API ключа, който добави в Render Environment (re_efxd9huo...)
        resend.api_key = os.getenv("RESEND_API_KEY")
        
        # При безплатния профил на Resend, писмата ЗАДЪЛЖИТЕЛНО се пращат от този адрес:
        self.sender_email = os.getenv("ALERT_EMAIL_FROM", "onboarding@resend.dev")
        
        # Минимален толеранс за риск и скоринг лимити
        self.MIN_SCORING_THRESHOLD = 65  
        self.MAX_ALLOWED_DEV_HOLDING = 15.0  
        self.MIN_LIQUIDITY_USD = 5000        

    def fetch_market_data(self) -> List[Dict[str, Any]]:
        logging.info("Скениране на пазара за нови меме койни...")
        try:
            mock_tokens = [
                {
                    "symbol": "DOGEPLUS",
                    "address": "0x123...abc",
                    "liquidity": 12000,
                    "dev_holding_pct": 8.5,
                    "is_mintable": False,
                    "has_renounced_ownership": True,
                    "price_candles": [1.0, 1.2, 1.5, 1.4, 1.8]
                },
                {
                    "symbol": "SCAMCOIN",
                    "address": "0x987...xyz",
                    "liquidity": 2000,
                    "dev_holding_pct": 45.0, 
                    "is_mintable": True,      
                    "has_renounced_ownership": False,
                    "price_candles": [1.0, 0.5, 0.2]
                }
            ]
            return mock_tokens
        except Exception as e:
            logging.error(f"Грешка при извличане на пазарни данни: {e}")
            return []

    def check_rug_signals(self, token: Dict[str, Any]) -> Dict[str, Any]:
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
        if len(price_history) < 3:
            return "Недостатъчно данни за технически анализ"
            
        if price_history[-1] > price_history[-2] > price_history[-3]:
            return "СИЛЕН БИЧИ ТРЕНД (Bullish Momentum)"
        elif price_history[-1] < price_history[-2]:
            return "Мечи тренд (Bearish / Разпродажба)"
        return "Странично движение (Consolidation)"

    def calculate_total_score(self, token: Dict[str, Any], security: Dict[str, Any]) -> int:
        score = 50 
        if security["is_safe"]:
            score += 25
        else:
            score -= 30
            
        if token["liquidity"] > 10000:
            score += 15
            
        return max(0, min(100, score))

    def send_notification(self, message: str):
        logging.info(f"ИЗВЕСТИЕ ДО TELEGRAM: {message}")

    def send_email_notification(self, subject: str, body: str):
        """Изпращане на имейл чрез Resend API до всеки получател поотделно."""
        if not resend.api_key:
            logging.error("❌ Грешка: Променливата RESEND_API_KEY липсва в Render Environment!")
            return False

        if not self.receiver_emails:
            logging.error("❌ Грешка: ALERT_EMAIL_TO е празен или липсва в Render Environment!")
            return False

        successful = 0

        for recipient in self.receiver_emails:
            try:
                logging.info(f"📤 Изпращане на email до: {recipient}")

                params = {
                    "from": self.sender_email,
                    "to": [recipient],
                    "subject": subject,
                    "text": body,
                }

                r = resend.Emails.send(params)
                email_id = r.get("id") if isinstance(r, dict) else None

                logging.info(
                    f"✅ Resend прие съобщението за {recipient} | ID: {email_id}"
                )

                successful += 1

            except Exception as e:
                logging.error(
                    f"❌ Грешка при изпращане до {recipient}: {e}"
                )

        if successful == len(self.receiver_emails):
            logging.info(
                f"📧 Успешно изпратено до всички {successful} получатели."
            )
            return True

        if successful > 0:
            logging.warning(
                f"⚠️ Изпратено до {successful}/{len(self.receiver_emails)} получатели."
            )
            return False

        logging.error("❌ Не беше изпратен email до нито един получател.")
        return False


    def monitor_market(self):
        print("🤖 Ultimate Memecoin Bot стартира успешно с Resend...")
        while True:
            tokens = self.fetch_market_data()
            
            for token in tokens:
                security_report = self.check_rug_signals(token)
                market_trend = self.analyze_candlesticks(token["price_candles"])
                final_score = self.calculate_total_score(token, security_report)
                
                if final_score >= self.MIN_SCORING_THRESHOLD:
                    alert_msg = (
                        f"🚨 ОТКРИТ ПОТЕНЦИАЛЕН ТОКЕН! 🚨\n\n"
                        f"🪙 Токен: #{token['symbol']}\n"
                        f"📊 Финална оценка: {final_score}/100\n"
                        f"📈 Пазарен анализ: {market_trend}\n"
                        f"💧 Ликвидност: ${token['liquidity']}\n"
                        f"🛡️ Сигурност: Проверен и безопасен за търговия."
                    )
                    self.send_notification(alert_msg)
                    
                    email_subject = f"🚨 БОТ СИГНАЛ: Намерен потенциален токен #{token['symbol']}"
                    self.send_email_notification(email_subject, alert_msg)
                else:
                    logging.info(f"Токен {token['symbol']} е отхвърлен. Рискове: {security_report['risks']}")
            
            time.sleep(60)

if __name__ == "__main__":
    bot = UltimateMemecoinBot()
    bot.monitor_market()  # Този ред стартира бота да върти цикъла на заден план!
