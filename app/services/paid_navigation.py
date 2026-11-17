"""Keep navigation from the purchase receipt scoped to that message."""
import re


def split(action):
    base, _, suffix = action.rpartition('~r')
    if re.fullmatch(r'[1-9]\d{0,18}', suffix):
        return base, int(suffix)
    return action, None


def route(action, sid):
    return f'{action}~r{sid}'


def contextualize(screen, action, sid):
    card = f'paid_receipt:{sid}'
    extra = f'subscription_extra:{sid}:paid'
    parent = card if action in (f'rename:{sid}', f'paid_help:{sid}', extra) else extra
    prefixes = (
        'paid_help:', 'subscription_extra:', 'rename:', 'devices:', 'device_rotate:',
        'device_rotate_apply:', 'vd:', 'vr:', 'history:', 'autorenew:', 'card_bind_info:',
        'renew:', 'renew_devices:', 'tariff:', 'catalog_groups:', 'catalog_table:',
        'ag:', 'up:', 'sub_control:', 'sub_apply:', 'checkout:', 'checkout_view:',
        'checkout_check:', 'checkout_balance:', 'order_pay:', 'order_check:', 'topup:',
        'topup_pay:', 'topup_check:', 'p:home', 'p:access:', 'p:list:',
    )
    options = {}
    for row in screen.rows:
        for index, (label, target) in enumerate(row):
            replacement = target
            if target == f'sub:{sid}' or (label.startswith('←') and target in ('home', 'subscriptions')):
                replacement = route(parent, sid) if parent == extra else parent
            elif target.startswith(prefixes):
                candidate = route(target, sid)
                if len(candidate.encode()) <= 64:
                    replacement = candidate
            if replacement != target:
                row[index] = label, replacement
                if target in screen.button_options:
                    options[replacement] = screen.button_options[target]
    screen.button_options.update(options)
    return screen
