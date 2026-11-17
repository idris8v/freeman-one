from aiogram.fsm.state import State, StatesGroup


class PromoInput(StatesGroup):
    waiting = State()


class AdminInput(StatesGroup):
    waiting = State()


class SubscriptionNameInput(StatesGroup):
    waiting = State()


class ExtendedInput(StatesGroup):
    waiting = State()


class TicketInput(StatesGroup):
    waiting = State()


class GiftInput(StatesGroup):
    waiting = State()


class TopupInput(StatesGroup):
    waiting = State()
