import asyncio
import logging
import random
import time
from typing import Dict, List
from app.services.notifications import notifier

log = logging.getLogger("ai_processor")

class AIProcessor:
    """
    Mock AI Processor for CamVision Advance.
    In a real implementation, this would use ultralytics YOLOv10.
    """
    def __init__(self):
        self.active_tasks: Dict[str, bool] = {}

    async def analyze_frame(self, cam_id: str) -> List[dict]:
        """
        Simulates AI inference on a camera frame.
        Returns bounding boxes and confidence scores.
        """
        # Simulate processing time
        await asyncio.sleep(0.05)
        
        # Randomly generate detections to simulate real-world behavior
        detections = []
        
        # 30% chance to detect a person
        if random.random() < 0.3:
            detections.append({
                "label": "person",
                "confidence": round(random.uniform(0.85, 0.99), 2),
                "box": [
                    random.randint(100, 300), # x
                    random.randint(100, 300), # y
                    random.randint(100, 200), # w
                    random.randint(200, 400)  # h
                ]
            })
            
        # 10% chance to detect a car
        if random.random() < 0.1:
            detections.append({
                "label": "vehicle",
                "confidence": round(random.uniform(0.75, 0.95), 2),
                "box": [
                    random.randint(400, 600),
                    random.randint(300, 500),
                    random.randint(200, 300),
                    random.randint(150, 250)
                ]
            })
            
        if detections:
            for det in detections:
                if det["label"] == "person" and det["confidence"] > 0.9:
                    notifier.send_telegram(f"<b>CamVision Alert</b>\nPerson detected on camera: {cam_id}\nConfidence: {det['confidence']*100}%")
            
        return detections

# Global processor instance
processor = AIProcessor()
