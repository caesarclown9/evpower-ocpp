# OCPP 1.6 WebSocket клиент для подключения к SteVe
import asyncio
import os
import websockets
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call_result
from ocpp.routing import on
import argparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

parser = argparse.ArgumentParser(description="OCPP 1.6 WebSocket клиент")
parser.add_argument("--chargebox_id", type=str, default=os.getenv("CHARGEBOX_ID", "DE-BERLIN-001"), help="ID станции (chargeBoxId)")
parser.add_argument("--ocpp_url", type=str, default=os.getenv("OCPP_URL", "ws://localhost:8180/ws/DE-BERLIN-001"), help="OCPP WebSocket URL")
args = parser.parse_args()

CHARGEBOX_ID = args.chargebox_id
OCPP_URL = args.ocpp_url

class ChargePoint(cp):
    @on('BootNotification')
    async def on_boot_notification(self, charge_point_model, charge_point_vendor, **kwargs):
        print(f"BootNotification: {charge_point_model}, {charge_point_vendor}")
        return call_result.BootNotificationPayload(
            current_time='2024-06-01T12:00:00Z',
            interval=10,
            status='Accepted'
        )

    @on('Heartbeat')
    async def on_heartbeat(self, **kwargs):
        from datetime import datetime
        print("Heartbeat received")
        return call_result.HeartbeatPayload(current_time=datetime.utcnow().isoformat())

async def main():
    async with websockets.connect(
        OCPP_URL,
        subprotocols=['ocpp1.6']
    ) as ws:
        charge_point = ChargePoint(CHARGEBOX_ID, ws)
        await charge_point.start()

if __name__ == '__main__':
    asyncio.run(main()) 