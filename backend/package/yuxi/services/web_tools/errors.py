class InvalidInputError(ValueError):
    status_code = 422

    def __init__(self, message, *, code='web_input_invalid'):
        super().__init__(message)
        self.code = code


class UpstreamError(InvalidInputError):
    status_code = 502


class OperationTimeoutError(UpstreamError):
    status_code = 504
