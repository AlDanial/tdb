#! /usr/bin/env python3
import asyncio


async def make_str():
    user_str = "abc  def"
    result = user_str.upper()
    await asyncio.sleep(3)
    print(f"make_str finished, result={result}")
    return result


async def sleeper():
    await asyncio.sleep(6)
    result = None
    print(f"sleeper finished, result={result}")
    return result


async def add_spaces(t6):
    val6 = await t6
    result = "".join(char + " " for char in val6)
    print(f"add_spaces finished, result={result}")
    return result


async def get_len(t6):
    val6 = await t6
    result = len(val6)
    print(f"get_len finished, result={result}")
    return result


async def wait_3(t3, t4, t5):
    await asyncio.gather(t3, t4, t5)
    result = None
    print(f"wait_3 finished, result={result}")
    return result


async def wait_2(t5, t6):
    await asyncio.gather(t5, t6)
    result = None
    print(f"wait_2 finished, result={result}")
    return result


async def tdb_pause_hook():
    # This function keeps the event loop alive gives tdb an
    # opportunity to pause the program.
    while True:
        await asyncio.sleep(1)


async def main():
    t6 = asyncio.create_task(make_str(), name="make_str")
    t5 = asyncio.create_task(sleeper(), name="sleeper")
    t4 = asyncio.create_task(add_spaces(t6), name="add_spaces")
    t3 = asyncio.create_task(get_len(t6), name="get_len")
    t2 = asyncio.create_task(wait_3(t3, t4, t5), name="wait_3")
    t1 = asyncio.create_task(wait_2(t5, t6), name="wait_2")

    tdb_pause_hook_task = asyncio.create_task(tdb_pause_hook(), name="PauseHook")
    try:
        # awaiting t1 and t2 implicitly waits for nested dependencies
        await asyncio.gather(t1, t2)
    finally:
        tdb_pause_hook_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
