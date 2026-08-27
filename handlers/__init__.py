"""Роутеры бота.

ПОРЯДОК ЗНАЧИМ. aiogram проверяет роутеры в порядке включения, а внутри
роутера — в порядке регистрации. fallback обязан идти последним: его
`F.text` перехватывает любое сообщение.
"""

from aiogram import Router

from . import admin, fallback, menu, payments, spreads

ROUTERS: tuple[Router, ...] = (
    payments.router,
    admin.router,
    menu.router,
    spreads.router,
    fallback.router,
)

__all__ = ["ROUTERS"]
