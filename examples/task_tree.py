#! /usr/bin/env python3
"""
Google Gemini 3.1 Pro
"""
import asyncio

async def task6():
    # input() is a blocking operation. We use asyncio.to_thread so it 
    # doesn't block the event loop, allowing Task 5 to start its sleep.
    user_str = await asyncio.to_thread(input, "Enter a string: ")
    result = user_str.upper()
    print(f"Task 6 finished, result={result}")
    return result

async def task5():
    await asyncio.sleep(10)
    result = None
    print(f"Task 5 finished, result={result}")
    return result

async def task4(t6):
    # Wait for task 6 to finish and get its return value
    val6 = await t6
    # Add a space after each character
    result = "".join(char + " " for char in val6)
    print(f"Task 4 finished, result={result}")
    return result

async def task3(t6):
    # Wait for task 6 to finish and get its return value
    val6 = await t6
    result = len(val6)
    print(f"Task 3 finished, result={result}")
    return result

async def task2(t3, t4, t5):
    # Wait on Tasks 3, 4, and 5
    await asyncio.gather(t3, t4, t5)
    result = None
    print(f"Task 2 finished, result={result}")
    return result

async def task1(t5, t6):
    # Wait on Tasks 5 and 6
    await asyncio.gather(t5, t6)
    result = None
    print(f"Task 1 finished, result={result}")
    return result

async def main():
    # 1. Create the base tasks (Tasks 5 and 6)
    t6 = asyncio.create_task(task6())
    t5 = asyncio.create_task(task5())
    
    # 2. Create tasks 3 and 4, passing the Task 6 object so they can await it
    t4 = asyncio.create_task(task4(t6))
    t3 = asyncio.create_task(task3(t6))
    
    # 3. Create tasks 1 and 2, passing their respective dependencies
    t2 = asyncio.create_task(task2(t3, t4, t5))
    t1 = asyncio.create_task(task1(t5, t6))
    
    # 4. Wait for the top-level tasks to finish.
    # Awaiting t1 and t2 implicitly waits for all other nested dependencies to complete.
    await asyncio.gather(t1, t2)

if __name__ == "__main__":
    # Run the main asyncio event loop
    asyncio.run(main())
