"""Minimal synchronous transaction ownership for an existing SQLAlchemy Session."""

from __future__ import annotations

from enum import Enum
from typing import Any

from app.core.transaction_errors import (
    PersistenceOperationError,
    PersistenceOutcomeUnknownError,
    TransactionSessionUnusableError,
)


class TransactionPhase(str, Enum):
    PRE_COMMIT = "pre_commit"
    COMMITTING = "committing"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


_OWNER_ATTRIBUTE = "_aura_transaction_owner"
_UNUSABLE_ATTRIBUTE = "_aura_transaction_session_unusable"


def mark_session_unusable(session: Any) -> None:
    setattr(session, _UNUSABLE_ATTRIBUTE, True)


def is_session_unusable(session: Any) -> bool:
    try:
        return vars(session).get(_UNUSABLE_ATTRIBUTE) is True
    except TypeError:
        return False


def _current_owner(session: Any):
    try:
        return vars(session).get(_OWNER_ATTRIBUTE)
    except TypeError:
        return None


class UnitOfWork:
    """Own exactly one commit boundary without owning the Session lifetime."""

    def __init__(self, session: Any):
        self.session = session
        self.phase = TransactionPhase.PRE_COMMIT
        self._entered = False

    def __enter__(self) -> "UnitOfWork":
        # A UnitOfWork represents one transaction boundary and is single-use.
        # Reject re-entry before consulting or mutating any Session state.
        if self.phase is not TransactionPhase.PRE_COMMIT or self._entered:
            raise PersistenceOperationError()
        if is_session_unusable(self.session):
            raise TransactionSessionUnusableError()
        if _current_owner(self.session) is not None:
            raise PersistenceOperationError()
        try:
            setattr(self.session, _OWNER_ATTRIBUTE, self)
        except (AttributeError, TypeError):
            raise PersistenceOperationError() from None
        self._entered = True
        return self

    def commit(self) -> None:
        if not self._entered or self.phase is not TransactionPhase.PRE_COMMIT:
            raise PersistenceOperationError()
        self.phase = TransactionPhase.COMMITTING
        try:
            self.session.commit()
        except BaseException as error:
            # A driver failure while COMMIT is in flight has an indeterminate
            # outcome. A rollback cannot prove that the server did not commit.
            mark_session_unusable(self.session)
            raise PersistenceOutcomeUnknownError() from error
        self.phase = TransactionPhase.COMMITTED

    def _rollback_confirmed_pre_commit(self) -> None:
        try:
            self.session.rollback()
        except BaseException as error:
            mark_session_unusable(self.session)
            raise TransactionSessionUnusableError() from error
        self.phase = TransactionPhase.ROLLED_BACK

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            if self.phase is TransactionPhase.PRE_COMMIT:
                self._rollback_confirmed_pre_commit()
                if exc_value is None:
                    raise PersistenceOperationError()
                if isinstance(exc_value, Exception):
                    if isinstance(
                        exc_value,
                        (
                            PersistenceOperationError,
                            PersistenceOutcomeUnknownError,
                            TransactionSessionUnusableError,
                        ),
                    ):
                        return False
                    raise PersistenceOperationError() from exc_value
                # Preserve cancellation and other BaseException control flow
                # after performing mandatory rollback cleanup.
                return False
            # COMMITTING means commit() raised an unknown-outcome exception.
            # COMMITTED must never be rolled back by this scope.
            return False
        finally:
            if _current_owner(self.session) is self:
                delattr(self.session, _OWNER_ATTRIBUTE)
            self._entered = False
