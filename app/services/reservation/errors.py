"""Safe public reservation API errors without identity details."""


class ReservationReferenceRequestError(ValueError):
    code = "INVALID_RESERVATION_REFERENCE"

    def __init__(self) -> None:
        super().__init__(self.code)


class ReservationNotFoundError(LookupError):
    code = "RESERVATION_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__(self.code)


class PublicReservationContractError(RuntimeError):
    code = "RESERVATION_REFERENCE_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(self.code)


class PastReservationDateError(ValueError):
    code = "PAST_RESERVATION_DATE"

    def __init__(self) -> None:
        super().__init__(self.code)


class PastReservationTimeError(ValueError):
    code = "PAST_RESERVATION_TIME"

    def __init__(self) -> None:
        super().__init__(self.code)
