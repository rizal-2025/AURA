REQUIRED_FIELDS = [
    "name",
    "people",
    "date",
    "time",
]


class ConversationState:

    @staticmethod
    def get_missing_fields(data: dict):

        missing = []

        for field in REQUIRED_FIELDS:

            if field not in data or data[field] in ("", None):
                missing.append(field)

        return missing

    @staticmethod
    def next_question(field: str):

        questions = {
            "name": "Atas nama siapa reservasinya?",
            "people": "Reservasi untuk berapa orang?",
            "date": "Reservasi untuk tanggal berapa?",
            "time": "Jam berapa?"
        }

        return questions.get(field, "Mohon lengkapi data reservasi.")