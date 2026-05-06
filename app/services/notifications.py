import logging
import requests
from app.core.config import config

log = logging.getLogger("notifications")

class NotificationManager:
    def __init__(self, telegram_token: str = config.TELEGRAM_BOT_TOKEN, 
                 telegram_chat_id: str = config.TELEGRAM_CHAT_ID):
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id

    def send_telegram(self, message: str):
        if not self.telegram_token or not self.telegram_chat_id:
            log.warning("Telegram notifications not configured")
            return
        
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        try:
            res = requests.post(url, json={
                "chat_id": self.telegram_chat_id,
                "text": message,
                "parse_mode": "HTML"
            })
            res.raise_for_status()
        except Exception as e:
            log.error(f"Failed to send Telegram notification: {e}")

    def send_web_push(self, title: str, body: str):
        # Skeleton for Web Push (using pywebpush)
        log.info(f"Web Push: {title} - {body}")

# Instance
notifier = NotificationManager()
