"""Shared byte counters for VPN and Telegram Proxy screens."""


def traffic(value):
    value = int(value or 0)
    for unit in ('Б', 'КБ', 'МБ', 'ГБ', 'ТБ'):
        if value < 1024 or unit == 'ТБ':
            return f'{value:.1f} {unit}' if unit != 'Б' else f'{value} Б'
        value /= 1024
