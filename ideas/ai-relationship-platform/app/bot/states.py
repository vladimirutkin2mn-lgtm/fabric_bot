"""Transient aiogram FSM hints; durable progress remains in PostgreSQL."""

from aiogram.fsm.state import State, StatesGroup


class OnboardingStates(StatesGroup):
    waiting_for_age = State()
    waiting_for_consent = State()


class IntakeStates(StatesGroup):
    waiting_for_conversation = State()
    waiting_for_goal = State()
