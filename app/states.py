from aiogram.fsm.state import State, StatesGroup


class BroadcastStates(StatesGroup):
    entering_text = State()
    confirm = State()
