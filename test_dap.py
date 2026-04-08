"""Diagnostic: test the DAP client with correct protocol sequence."""

import asyncio
from pathlib import Path
from tdb.dap.client import DAPClient

async def main():
    client = DAPClient()
    client.on_event("*", lambda e: print(f"  EVENT: {e.event}"))

    print("1. Starting debugpy adapter...")
    await client.start()

    print("2. Sending initialize...")
    await client.initialize()
    print("   OK")

    print("3. Sending launch (fire-and-forget)...")
    launch_future = await client.launch(
        program=str(Path("test_script.py").resolve()),
        cwd=str(Path(".").resolve()),
        stop_on_entry=True,
    )
    print("   Sent (not awaited)")

    print("4. Waiting for initialized event...")
    await asyncio.sleep(1)

    print("5. Sending configurationDone...")
    await client.configuration_done()
    print("   OK")

    print("6. Awaiting launch response...")
    response = await asyncio.wait_for(launch_future, timeout=10.0)
    print(f"   OK (success={response.success})")

    print("7. Waiting for stopped event...")
    await asyncio.sleep(1)

    print("8. Fetching threads...")
    threads = await client.threads()
    for t in threads:
        print(f"   Thread {t.id}: {t.name}")

    if threads:
        tid = threads[0].id
        print("9. Fetching stack trace...")
        frames = await client.stack_trace(tid)
        for f in frames:
            src = f.source.path if f.source else "?"
            print(f"   {f.name} at {src}:{f.line}")

        print("10. Sending continue...")
        await client.continue_(tid)
        await asyncio.sleep(2)

    print("11. Disconnecting...")
    await client.disconnect()
    await client.stop()
    print("    Done!")

asyncio.run(main())
