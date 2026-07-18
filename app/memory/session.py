class ConversationMemory:
    def __init__(self):
        self.sessions = {}

    def get(self, session_id: str):
        return self.sessions.setdefault(session_id, {})

    def update(self, session_id: str, data: dict):

        session = self.sessions.setdefault(session_id, {})

        for key, value in data.items():

            if value is not None:

                session[key] = value

    def clear(self, session_id: str):
        self.sessions.pop(session_id, None)


memory = ConversationMemory()