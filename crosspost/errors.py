class PlatformError(Exception):
    """Ошибка площадки.

    retryable: можно повторить (сеть, 5xx, rate limit).
    uncertain: запрос мог пройти, повтор создаст дубль. Не повторяем, сообщаем.
    """

    def __init__(self, message: str, retryable: bool = False, uncertain: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.uncertain = uncertain
