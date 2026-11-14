import multiprocessing
import sys


def child() -> None:
    print("-".join(["child", "says", "hi"]), flush=True)
    sys.exit(3)


if __name__ == "__main__":
    print("-".join(["parent", "start"]), flush=True)
    multiprocessing.set_start_method("spawn")
    p = multiprocessing.Process(target=child)
    p.start()
    p.join()
    print("-".join(["child", "code", str(p.exitcode)]), flush=True)
    sys.exit(5)
