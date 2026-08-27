"""Замок против второго экземпляра бота.

Два процесса с одним токеном — это не «просто дубли»: Telegram отдаёт апдейт
то одному, то другому, человек получает по два ответа, а свечи списываются
дважды. На сервере это происходит буднично — systemd поднял сервис, а рядом
кто-то запустил бота руками, чтобы «посмотреть логи».

Замок держится ОС и снимается сам, если процесс умер, — то есть после
принудительного kill или падения питания перезапуск не требует уборки руками.
"""

import logging
import os
import sys

logger = logging.getLogger("mira_bot")

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# Windows блокирует диапазон байт и запрещает запись в него — включая запись
# от самого владельца замка. Поэтому замок ставится на далёкий байт, в который
# мы никогда не пишем, а PID живёт в начале файла.
_LOCK_OFFSET = 1 << 20
# PID пишется в поле фиксированной ширины: файл не усекается (усечение задело
# бы заблокированный байт), поэтому старое значение нужно перекрывать целиком.
_PID_FIELD = 20


class AlreadyRunning(RuntimeError):
    """Второй экземпляр на том же файле-замке."""


class SingleInstance:
    """Файловый замок. Использовать как контекстный менеджер вокруг main()."""

    def __init__(self, path: str):
        self.path = path
        self._fh = None

    def _acquire(self) -> None:
        if sys.platform == "win32":
            self._fh.seek(_LOCK_OFFSET)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(self) -> None:
        if sys.platform == "win32":
            self._fh.seek(_LOCK_OFFSET)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)

    def _holder(self) -> str:
        try:
            self._fh.seek(0)
            return self._fh.read(_PID_FIELD).strip() or "неизвестен"
        except OSError:
            return "неизвестен"

    def __enter__(self) -> "SingleInstance":
        # os.open, а не open(..., "a+"): в режиме дозаписи любая запись уходит
        # в конец файла независимо от seek, и PID накапливался бы строками.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        self._fh = os.fdopen(fd, "r+", encoding="utf-8")

        try:
            self._acquire()
        except OSError:
            holder = self._holder()
            self._fh.close()
            self._fh = None
            raise AlreadyRunning(
                f"Бот уже запущен (PID {holder}), файл-замок: {self.path}"
            ) from None

        self._fh.seek(0)
        self._fh.write(f"{os.getpid()}".ljust(_PID_FIELD))
        self._fh.flush()
        logger.info(f"🔒 Замок взят: {self.path} (PID {os.getpid()})")
        return self

    def __exit__(self, *_exc) -> None:
        if not self._fh:
            return
        try:
            self._release()
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None
            # Файл не удаляем: на Linux его мог уже перехватить новый процесс,
            # и удаление сняло бы чужой замок.
