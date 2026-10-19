import asyncio
from pathlib import Path

from pyatv import pair, scan
from pyatv.const import Protocol

PIN_FILE = Path("/tmp/opencode/pin.txt")
CRED_FILE = Path("/tmp/opencode/airplay_creds.txt")


async def main() -> None:
    PIN_FILE.unlink(missing_ok=True)
    CRED_FILE.unlink(missing_ok=True)

    print("scanning for AirPlay at 192.168.1.160…", flush=True)
    loop = asyncio.get_event_loop()
    atvs = await asyncio.wait_for(
        scan(loop, identifier=None, protocol=Protocol.AirPlay, hosts=["192.168.1.160"]), timeout=20
    )
    if not atvs:
        CRED_FILE.write_text("ERROR: no AirPlay device found at 192.168.1.160")
        return
    device = atvs[0]
    print(f"found: {device.name} {device.device_info}", flush=True)

    service = device.get_service(Protocol.AirPlay)
    pairing = await pair(device, Protocol.AirPlay, loop)
    try:
        await pairing.begin()
        print("PAIRING_STARTED — waiting for PIN…", flush=True)

        pin = None
        max_wait_seconds = 360
        for _ in range(max_wait_seconds):
            if PIN_FILE.exists():
                raw = PIN_FILE.read_text().strip()
                if raw.isdigit():
                    pin = int(raw)
                    break
            await asyncio.sleep(1)
        if pin is None:
            CRED_FILE.write_text("ERROR: no PIN provided within 6 minutes")
            return

        pairing.pin(pin)
        await pairing.finish()
        if pairing.has_paired:
            creds = pairing.service.credentials
            CRED_FILE.write_text(f"OK {device.name} {creds}")
            print(f"PAIRED! credentials={creds}", flush=True)
        else:
            CRED_FILE.write_text("ERROR: pairing failed (wrong code or rejected)")
            print("pairing failed", flush=True)
    finally:
        await pairing.close()


asyncio.run(main())
