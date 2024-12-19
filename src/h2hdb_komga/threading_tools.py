import threading

from h2hdb.threading_tools import ThreadsList


KOMGA_SEMAPHORE = threading.Semaphore(10)


class KomgaThreadsList(ThreadsList):
    def get_semaphores(self) -> list[threading.Semaphore]:
        return [KOMGA_SEMAPHORE]
