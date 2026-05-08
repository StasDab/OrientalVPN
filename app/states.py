from aiogram.fsm.state import State, StatesGroup


class BroadcastStates(StatesGroup):
    entering_text = State()
    confirm = State()


class TopupStates(StatesGroup):
    waiting_amount = State()


class PromoStates(StatesGroup):
    waiting_code = State()
